import { afterEach, expect, it, vi } from 'vitest';
import { LiveStore } from '../src/live';
import { parseFrame } from '../src/pipeline';
import type { Connection } from '../src/transport';
import type { EventSequence } from '../src/sequence';

class Parser {
  static instances: Parser[] = [];
  onmessage?: (event: { data: unknown }) => void;
  onerror?: () => void;
  messages: { raw: string; generation: number }[] = [];
  terminated = false;
  constructor() {
    Parser.instances.push(this);
  }
  postMessage(message: { raw: string; generation: number }) {
    this.messages.push(message);
  }
  complete() {
    const message = this.messages.shift()!;
    try {
      this.onmessage?.({
        data: { frame: parseFrame(message.raw), generation: message.generation },
      });
    } catch {
      this.onmessage?.({ data: { error: 'Invalid input', generation: message.generation } });
    }
  }
  terminate() {
    this.terminated = true;
  }
}
function fixture() {
  Parser.instances = [];
  vi.stubGlobal('Worker', Parser);
  vi.stubGlobal('requestAnimationFrame', (fn: () => void) => setTimeout(fn, 0));
  vi.stubGlobal('cancelAnimationFrame', clearTimeout);
  let receive!: Parameters<Connection['subscribe']>[1];
  let connectionState!: Parameters<Connection['subscribe']>[2];
  const cursors: EventSequence[] = [];
  const transport: Connection = {
    native: false,
    async connect() {},
    async pair() {},
    async revokePairing() {},
    async request() {
      throw Error('Unexpected HTTP');
    },
    async audio() {
      throw Error('Unexpected audio');
    },
    async openLegacy() {},
    async subscribe(after: EventSequence, callback: typeof receive, state: typeof connectionState) {
      receive = callback;
      connectionState = state;
      cursors.push(after);
      return () => {};
    },
  };
  const store = new LiveStore(transport);
  return {
    store,
    transport,
    cursors,
    deliver: (raw: string) => Promise.resolve(receive(raw)),
    parser: () => Parser.instances.at(-1)!,
    failTransport: () => connectionState(false, 'Native delivery interrupted — resynchronizing'),
  };
}
function event(sequence: bigint, id = sequence.toString()) {
  return (
    '{"contract":"openradioinspector-platform","contract_version":1,"data":{"sequence":' +
    sequence +
    ',"event_type":"sweep.received","payload":{"sweep":' +
    JSON.stringify({
      id,
      session_id: 's',
      stream_id: 'a',
      interval: {
        started_at: '2026-11-11T00:00:00.123456Z',
        ended_at: '2026-11-11T00:00:01.123456Z',
      },
      frequencies_hz: [100],
      levels: [-0],
      level_unit: 'dBm',
      settings: {},
    }) +
    '}}}'
  );
}
afterEach(() => vi.unstubAllGlobals());

it('applies a slow replay of more than 1000 events with one worker item and exact applied cursor', async () => {
  const { store, deliver, parser } = fixture();
  store.scope('s', 'a', 'r');
  const initial = 9007199254740992n;
  await store.start(initial);
  let applied = false;
  const first = Promise.resolve(deliver(event(initial + 1n))).then(() => {
    applied = true;
  });
  await Promise.resolve();
  expect(applied).toBe(false);
  expect(store.isApplying()).toBe(true);
  expect(parser().messages).toHaveLength(1);
  parser().complete();
  await first;
  expect(store.lastApplied()).toBe(initial + 1n);
  expect(store.isApplying()).toBe(false);
  for (let i = 2; i <= 1205; i++) {
    const done = deliver(event(initial + BigInt(i)));
    expect(parser().messages).toHaveLength(1);
    parser().complete();
    await done;
  }
  await vi.waitFor(() => expect(store.getSnapshot().rows).toHaveLength(256));
  expect(store.lastApplied()).toBe(initial + 1205n);
  expect(store.getSnapshot().rows[0].id).toBe((initial + 950n).toString());
  expect(store.getSnapshot().gaps).toBe(0);
  const duplicate = deliver(event(initial + 1205n));
  parser().complete();
  await duplicate;
  expect(store.lastApplied()).toBe(initial + 1205n);
  store.dispose();
});

it('does not acknowledge invalid committed data or let a late seed replace live evidence', async () => {
  const { store, deliver, parser } = fixture();
  store.scope('s', 'a', 'r');
  await store.start(0);
  const invalid = deliver('invalid JSON');
  const rejected = expect(invalid).rejects.toThrow(/invalid/i);
  parser().complete();
  await rejected;
  expect(store.lastApplied()).toBe(0);
  await store.start(store.lastApplied());
  const raw = JSON.parse(event(1n, 'future-seed')).data.payload.sweep;
  store.seed([raw], []);
  const first = deliver(event(1n, 'first-committed'));
  parser().complete();
  await first;
  store.seed([{ ...raw, id: 'late-seed' }], []);
  await vi.waitFor(() =>
    expect(store.getSnapshot().rows.map((row) => row.id)).toEqual(['first-committed']),
  );
  const old = parser(),
    cancelled = deliver(event(2n, 'old-epoch'));
  const aborted = expect(cancelled).rejects.toMatchObject({ name: 'AbortError' });
  store.resetArchive();
  await aborted;
  await store.start(0);
  old.complete();
  expect(store.lastApplied()).toBe(0);
  const replacement = deliver(event(1n, 'new-epoch'));
  parser().complete();
  await replacement;
  await vi.waitFor(() =>
    expect(store.getSnapshot().rows.map((row) => row.id)).toEqual(['new-epoch']),
  );
  store.dispose();
});

it('requests replay if native acknowledgement fails instead of leaving a stopped subscription idle', async () => {
  const { store, deliver, parser, failTransport } = fixture();
  store.scope('s', 'a', 'r');
  await store.start(10);
  const applied = deliver(event(11n));
  parser().complete();
  await applied;
  const recover = vi.fn();
  store.onGap = recover;
  failTransport();
  expect(recover).toHaveBeenCalledOnce();
  expect(store.lastApplied()).toBe(11);
  await vi.waitFor(() =>
    expect(store.getSnapshot().state).toContain('Native delivery interrupted'),
  );
  store.dispose();
});

it('restores a bounded in-memory checkpoint without changing float bits or a large cursor', async () => {
  const { store, transport, deliver, parser } = fixture();
  expect(store.checkpoint()).toBeUndefined();
  expect(Parser.instances).toHaveLength(0);
  store.scope('s', 'a', 'r');
  store.seed([], []);
  await store.start(9007199254740992n);
  const pending = deliver(
    event(9007199254740993n, 'checkpoint').replace('"levels":[0]', '"levels":[-0.0]'),
  );
  parser().complete();
  await pending;
  const checkpoint = store.checkpoint()!;
  store.dispose();
  const restored = new LiveStore(transport);
  restored.restore(checkpoint);
  expect(restored.lastApplied()).toBe(9007199254740993n);
  expect(restored.needsSeed()).toBe(false);
  await vi.waitFor(() => expect(restored.getSnapshot().rows).toHaveLength(1));
  expect(Object.is(restored.getSnapshot().rows[0].levels[0], -0)).toBe(true);
  restored.scope('different', 'a', 'r');
  expect(restored.needsSeed()).toBe(true);
  restored.resetArchive(0);
  await vi.waitFor(() => expect(restored.getSnapshot().rows).toEqual([]));
  restored.dispose();
});

it('replays from the last applied cursor after a gap and ignores cancelled parser generations', async () => {
  const { store, deliver, parser, cursors } = fixture();
  store.scope('s', 'a', 'r');
  await store.start(5);
  const sixth = deliver(event(6n));
  parser().complete();
  await sixth;
  const gap = deliver(event(8n));
  const rejected = expect(gap).rejects.toThrow(/gap/i);
  parser().complete();
  await rejected;
  expect(store.lastApplied()).toBe(6);
  await store.start(store.lastApplied());
  const old = parser();
  const seventh = deliver(event(7n));
  const cancelled = expect(seventh).rejects.toMatchObject({ name: 'AbortError' });
  store.stop();
  await cancelled;
  await store.start(store.lastApplied());
  old.complete();
  expect(store.lastApplied()).toBe(6);
  const next = deliver(event(7n));
  parser().complete();
  await next;
  expect(cursors).toEqual([5, 6, 6]);
  expect(store.lastApplied()).toBe(7);
  store.resetArchive(0);
  expect(store.lastApplied()).toBe(0);
  await vi.waitFor(() => expect(store.getSnapshot().rows).toEqual([]));
  store.dispose();
});

it('receipts every selected append in a coalesced publication, excluding duplicates and other scopes', async () => {
  const { store, deliver, parser } = fixture();
  store.scope('s', 'a', 'r');
  await store.start(0);
  for (const [sequence, id] of [
    [1n, 'A'],
    [2n, 'B'],
    [3n, 'C'],
  ] as const) {
    const done = deliver(event(sequence, id));
    parser().complete();
    expect(await done).toBe(Number(sequence));
  }
  await vi.waitFor(() =>
    expect(store.getSnapshot().rows.map((row) => row.id)).toEqual(['A', 'B', 'C']),
  );
  const receipt = store.getSnapshot().presentation;
  expect(receipt.total).toBe(3n);
  for (const raw of [
    event(3n, 'C'),
    event(4n, 'C'),
    event(5n, 'wrong').replace('"stream_id":"a"', '"stream_id":"other"'),
  ]) {
    const done = deliver(raw);
    parser().complete();
    await done;
  }
  await vi.waitFor(() => expect(store.getSnapshot().sequence).toBe(5));
  expect(store.getSnapshot().rows.map((row) => row.id)).toEqual(['A', 'B', 'C']);
  expect(store.getSnapshot().presentation).toBe(receipt);
  store.dispose();
});

it('treats seed and restored rows as baseline, including a same-ID restore into an existing store', async () => {
  const { store, transport, deliver, parser } = fixture();
  store.scope('s', 'a', 'r');
  store.seed([JSON.parse(event(1n, 'seed')).data.payload.sweep], []);
  await store.start(0);
  await vi.waitFor(() => expect(store.getSnapshot().rows[0]?.id).toBe('seed'));
  expect(store.getSnapshot().presentation.total).toBe(0n);
  const seededEpoch = store.getSnapshot().presentation.epoch;
  const done = deliver(event(1n, 'committed'));
  parser().complete();
  await done;
  await vi.waitFor(() => expect(store.getSnapshot().rows[0]?.id).toBe('committed'));
  expect(store.getSnapshot().presentation.total).toBe(1n);
  expect(store.getSnapshot().presentation.epoch).not.toBe(seededEpoch);
  const original = store.getSnapshot().presentation;
  const checkpoint = store.checkpoint()!;
  expect(checkpoint).not.toHaveProperty('presentation');
  store.restore(checkpoint);
  await vi.waitFor(() => expect(store.getSnapshot().presentation.epoch).not.toBe(original.epoch));
  expect(store.getSnapshot().rows[0]).toBe(checkpoint.rows[0]);
  expect(store.getSnapshot().presentation.total).toBe(0n);
  const restored = new LiveStore(transport);
  restored.restore(checkpoint);
  await vi.waitFor(() => expect(restored.getSnapshot().rows).toHaveLength(1));
  expect(restored.getSnapshot().presentation.total).toBe(0n);
  expect(restored.getSnapshot().presentation.epoch).not.toBe(
    store.getSnapshot().presentation.epoch,
  );
  store.dispose();
  restored.dispose();
});

it('fences same-ID archive resets and unsupported plots but preserves same-scope reconnect receipts', async () => {
  const { store, deliver, parser } = fixture();
  store.scope('s', 'a', 'r');
  await store.start(0);
  const first = deliver(event(1n, 'same-id'));
  parser().complete();
  await first;
  await vi.waitFor(() => expect(store.getSnapshot().rows).toHaveLength(1));
  const receipt = store.getSnapshot().presentation;
  await store.start(store.lastApplied());
  store.scope('s', 'a', 'different-receiver');
  await vi.waitFor(() => expect(store.getSnapshot().state).toBe('Connecting'));
  expect(store.getSnapshot().presentation).toBe(receipt);
  const unsupported = deliver(
    event(2n, 'not-plotted').replace(
      '"frequencies_hz":[100]',
      '"frequencies_hz":[9007199254740993]',
    ),
  );
  parser().complete();
  await unsupported;
  await vi.waitFor(() => expect(store.getSnapshot().unsupported?.id).toBe('not-plotted'));
  expect(store.getSnapshot().presentation.total).toBe(0n);
  expect(store.getSnapshot().presentation.epoch).not.toBe(receipt.epoch);
  const beforeReset = store.getSnapshot().presentation;
  store.resetArchive();
  await store.start(0);
  const same = deliver(event(1n, 'same-id'));
  parser().complete();
  await same;
  // The old published row deliberately has the same ID. Wait for the reset
  // cursor's publication rather than mistaking that old row for completion.
  await vi.waitFor(() => expect(store.getSnapshot().sequence).toBe(1));
  await vi.waitFor(() => expect(store.getSnapshot().rows[0]?.id).toBe('same-id'));
  expect(store.getSnapshot().presentation.total).toBe(1n);
  expect(store.getSnapshot().presentation.epoch).not.toBe(beforeReset.epoch);
  const resetReceipt = store.getSnapshot().presentation;
  store.scope('other-session', 'a', 'r');
  await vi.waitFor(() => expect(store.getSnapshot().rows).toHaveLength(0));
  expect(store.getSnapshot().presentation.total).toBe(0n);
  expect(store.getSnapshot().presentation.epoch).not.toBe(resetReceipt.epoch);
  store.dispose();
});
