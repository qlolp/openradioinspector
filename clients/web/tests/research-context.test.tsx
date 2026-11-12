// @vitest-environment happy-dom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { Research } from '../src/Research';
import { setLocale } from '../src/i18n';
import { useDesk } from '../src/useDesk';
import { parseFrame, type FrameInput } from '../src/pipeline';
import type { Connection } from '../src/transport';
import type { Comparison, Health, MonitoringEvent, Project, Sweep } from '../src/types';
import type { EventSequence } from '../src/sequence';

// Graphics need a real browser; state, hooks, API envelopes and response ordering
// below are real. Assertions target Research/App DOM, never this drawing stub.
vi.mock('../src/charts', () => ({ Spectrum: () => null, Waterfall: () => null }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const envelope = (data: unknown) => ({
  contract: 'openradioinspector-platform',
  contract_version: 1,
  data,
});
const sweep = (id: string): Sweep => ({
  id,
  session_id: 'session',
  stream_id: 'analyzer',
  interval: {
    started_at: '2026-11-11T00:00:00.123456Z',
    ended_at: '2026-11-11T00:00:01.123456Z',
  },
  frequencies_hz: [100_000_000, 100_001_000],
  levels: [-90, -70],
  level_unit: 'dBm',
  settings: {},
});
const project: Project = {
  id: 'project',
  name: 'Owned component fixture',
  site: 'Synthetic',
  state: 'draft',
  active_session_id: 'session',
  analyzer_stream_id: 'analyzer',
  receiver_stream_id: 'receiver',
  baseline_id: 'baseline',
};
const otherProject: Project = {
  ...project,
  id: 'other',
  name: 'Other owned project',
  active_session_id: 'other-session',
};
function ProjectHost({ transport }: { transport: Connection }) {
  const desk = useDesk(transport);
  return (
    <>
      <button onClick={() => desk.setProjectId(otherProject.id)}>Select other project</button>
      <output data-testid="action-state">{desk.action.state + ' ' + desk.action.text}</output>
      <output data-testid="busy-state">{String(desk.busy)}</output>
      <output data-testid="health-cursor">{String(desk.health?.last_event_id ?? '')}</output>
      <output data-testid="live-cursor">{String(desk.live.sequence)}</output>
      <output data-testid="archive-generation">{String(desk.connectionVersion)}</output>
      <button onClick={() => void desk.resync()}>Request replay</button>
      <button onClick={() => void desk.connect('http://127.0.0.1:18771')}>
        Connect another owned node
      </button>
      <Research desk={desk} openOriginal={() => {}} />
    </>
  );
}
function comparison(id: string): Comparison {
  return {
    compatible: true,
    baseline_id: 'baseline',
    sweep_id: id,
    items: [
      {
        frequency_hz: 100_000_000,
        current_dbm: -90,
        baseline_p50_dbm: -95,
        baseline_p95_dbm: -91,
        delta_db: 5,
      },
      {
        frequency_hz: 100_001_000,
        current_dbm: -70,
        baseline_p50_dbm: -90,
        baseline_p95_dbm: -85,
        delta_db: 20,
      },
    ],
  };
}
class CoreFixture {
  highWater: EventSequence = 100;
  restored = false;
  comparison = deferred<Comparison>();
  comparisonSignal?: AbortSignal;
  heldHistory?: ReturnType<typeof deferred<unknown>>;
  historySignal?: AbortSignal;
  heldOriginal?: ReturnType<typeof deferred<unknown>>;
  originalSignal?: AbortSignal;
  heldSources?: ReturnType<typeof deferred<unknown>>;
  sourcesSignal?: AbortSignal;
  heldReview?: ReturnType<typeof deferred<unknown>>;
  reviewSignal?: AbortSignal;
  heldAudio?: ReturnType<typeof deferred<Blob>>;
  audioSignal?: AbortSignal;
  healthReplies: ReturnType<typeof deferred<Health>>[] = [];
  connectHighWater?: EventSequence;
  receive?: Parameters<Connection['subscribe']>[1];
  cursors: EventSequence[] = [];
  state?: Parameters<Connection['subscribe']>[2];
  requests: { path: string; body: unknown }[] = [];
  event(): MonitoringEvent {
    return {
      id: 'event',
      project_id: project.id,
      kind: 'new_signal',
      state: 'confirmed',
      first_seen: '2026-11-11T00:00:00.123456Z',
      last_seen: '2026-11-11T00:00:01.123456Z',
      center_frequency_hz: 100_001_000,
      start_frequency_hz: 100_000_000,
      end_frequency_hz: 100_001_000,
      peak_level: -70,
      source_count: 1,
      disposition: 'unreviewed',
      analysis: this.heldAudio ? { state: 'completed', audio: { id: 'owned-audio' } } : undefined,
      source_refs: this.heldSources
        ? undefined
        : [
            {
              sweep_id: this.restored ? 'fresh' : 'A',
              session_id: 'session',
              stream_id: 'analyzer',
            },
          ],
    };
  }
  transport: Connection = {
    native: false,
    connect: async () => {
      if (this.connectHighWater !== undefined) {
        this.highWater = this.connectHighWater;
        this.restored = true;
      }
    },
    pair: async () => {},
    revokePairing: async () => {},
    openLegacy: async () => {},
    audio: async (_path, signal) => {
      if (!this.heldAudio) throw Error('Unexpected audio');
      this.audioSignal = signal;
      return this.heldAudio.promise;
    },
    subscribe: async (after, receive, state) => {
      this.cursors.push(after);
      this.receive = receive;
      this.state = state;
      return () => {};
    },
    request: async (path, body, signal) => {
      this.requests.push({ path, body });
      const url = new URL(path, 'http://owned.invalid');
      const route = url.pathname.replace('/api/v1', '');
      if (route.endsWith('/events/event/review') && this.heldReview) {
        this.reviewSignal = signal;
        return envelope(await this.heldReview.promise);
      }
      if (body !== undefined) throw Error('No device/service commands are allowed in this fixture');
      if (route === '/health') {
        const held = this.healthReplies.shift();
        return envelope(held ? await held.promise : health(this.highWater));
      }
      if (route === '/capabilities')
        return envelope({ features: {}, limitations: [], hardware_validated: false });
      if (route === '/sessions')
        return envelope([{ id: 'session', name: 'Synthetic', state: 'running', streams: [] }]);
      if (route === '/monitoring-projects')
        return envelope({ items: [project, otherProject], next_cursor: null });
      if (route.endsWith('/status') && !route.endsWith('/search/status'))
        return envelope({
          project: route.includes('/other/') ? otherProject : project,
          active: false,
          receiver_busy: false,
          audio_configured: false,
          manual: [],
          workers: [],
          hardware_validation: 'not_run',
        });
      if (route.endsWith('/preflight'))
        return envelope({
          ready: false,
          checks: [],
          baseline_ready: false,
          baseline_collection: {
            sweeps: 0,
            elapsed_seconds: 0,
            minimum_sweeps: 20,
            minimum_seconds: 30,
            ready: false,
          },
          hardware_validation: 'not_run',
        });
      if (route.endsWith('/events')) return envelope({ items: [this.event()], next_cursor: null });
      if (route.endsWith('/events/event')) return envelope(this.event());
      if (route.endsWith('/events/event/sources') && this.heldSources) {
        this.sourcesSignal = signal;
        return envelope(await this.heldSources.promise);
      }
      if (route.endsWith('/allowed')) return envelope({ items: [], next_cursor: null });
      if (route.endsWith('/search/status'))
        return envelope({ active: false, coverage: 'paused', marks: [], series: [] });
      if (route.endsWith('/sweeps'))
        return envelope([sweep(this.restored ? 'fresh-live' : 'live')]);
      if (route.endsWith('/observations')) return envelope([]);
      if (route.endsWith('/comparison')) {
        this.comparisonSignal = signal;
        return envelope(await this.comparison.promise);
      }
      if (route.endsWith('/history')) {
        this.historySignal = signal;
        if (this.heldHistory) return envelope(await this.heldHistory.promise);
        return envelope({
          items: this.restored ? [sweep('fresh')] : [sweep('A'), sweep('B')],
          next_cursor: null,
        });
      }
      if (route.includes('/history/')) {
        this.originalSignal = signal;
        if (this.heldOriginal) return envelope(await this.heldOriginal.promise);
        return envelope(sweep(decodeURIComponent(route.split('/').at(-1)!)));
      }
      throw Error('Unexpected fixture route: ' + path);
    },
  };
  rollback() {
    this.highWater = 10;
    this.restored = true;
    // Actual transport disconnect notification; no event sequence is relabelled.
    this.state?.(false, 'Owned connection interrupted');
  }
  async deliver(sequence: EventSequence) {
    const source = JSON.stringify(sweep('committed-' + sequence));
    const raw =
      '{"contract":"openradioinspector-platform","contract_version":1,"data":' +
      '{"sequence":' +
      sequence +
      ',"event_type":"sweep.received","payload":{"sweep":' +
      source +
      '}}}';
    if (!this.receive) throw Error('Original subscription is not ready');
    return this.receive(raw);
  }
}
function health(sequence: EventSequence): Health {
  return { status: 'ok', last_event_id: sequence, sessions: 1, running_sessions: 0 };
}

let root: Root | undefined;
let container: HTMLDivElement;
async function settle() {
  for (let i = 0; i < 10; i++)
    await act(async () => {
      await Promise.resolve();
    });
}
async function click(element: HTMLElement) {
  await act(async () => element.click());
  await settle();
}
function button(text: string, within: ParentNode = container) {
  const result = [...within.querySelectorAll<HTMLButtonElement>('button')].find(
    (b) => b.textContent?.trim() === text,
  );
  if (!result) throw Error('Missing button: ' + text);
  return result;
}
function row(id: string) {
  const result = [...container.querySelectorAll<HTMLElement>('[data-testid="history-row"]')].find(
    (e) => e.querySelector('small')?.textContent?.endsWith(id),
  );
  if (!result) throw Error('Missing history row: ' + id);
  return result;
}
function action() {
  return container.querySelector<HTMLElement>('[data-testid="action-state"]')!.textContent!;
}
function busy() {
  const explicit = container.querySelector('[data-testid="busy-state"]');
  return explicit ? explicit.textContent === 'true' : button('＋ Prepare project').disabled;
}
async function mount(fixture: CoreFixture) {
  root = createRoot(container);
  await act(async () => root!.render(<App transport={fixture.transport} />));
  await settle();
}
async function load(fixture: CoreFixture) {
  await mount(fixture);
  await click(button('Research'));
  await click(button('Load history'));
  expect(row('A')).toBeTruthy();
}
async function rollback(fixture: CoreFixture) {
  await act(async () => {
    fixture.rollback();
    await vi.advanceTimersByTimeAsync(300);
  });
  await settle();
}
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal(
    'Worker',
    class {
      onmessage?: (message: { data: unknown }) => void;
      terminated = false;
      postMessage(message: { raw: FrameInput; generation: number }) {
        // Real strict parsing/preparation and LiveStore apply/ACK; only the
        // browser worker scheduling boundary is represented in this DOM host.
        queueMicrotask(() => {
          if (this.terminated) return;
          try {
            this.onmessage?.({
              data: { frame: parseFrame(message.raw), generation: message.generation },
            });
          } catch (error) {
            this.onmessage?.({ data: { error: String(error), generation: message.generation } });
          }
        });
      }
      terminate() {
        this.terminated = true;
      }
    },
  );
  let serial = 0;
  const cancelled = new Set<number>();
  vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => {
    const id = ++serial;
    queueMicrotask(() => {
      if (!cancelled.has(id)) fn(0);
    });
    return id;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => cancelled.add(id));
  localStorage.clear();
  setLocale('en');
  container = document.createElement('div');
  document.body.append(container);
});
afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  root = undefined;
  container.remove();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it('does not attach late comparison A to the newly selected source B', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await click(row('B').querySelector<HTMLElement>('.history-select')!);
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  expect(row('B').classList.contains('selected')).toBe(true);
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
  expect(fixture.comparisonSignal?.aborted).toBe(true);
});
it('keeps the positive comparison when the original selected source is unchanged', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  expect(row('A').classList.contains('selected')).toBe(true);
  expect(container.textContent).toContain('Baseline comparison · compatible');
});
it('preserves an explicit incompatible-baseline response for its unchanged source', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await act(async () =>
    fixture.comparison.resolve({
      ...comparison('A'),
      compatible: false,
      reason: 'Original profile mismatch',
      items: [],
    }),
  );
  await settle();
  expect(row('A').classList.contains('selected')).toBe(true);
  expect(container.textContent).toContain('Original profile mismatch');
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
});
it('invalidates the original project comparison when its real hook changes selection', async () => {
  const fixture = new CoreFixture();
  root = createRoot(container);
  await act(async () => root!.render(<ProjectHost transport={fixture.transport} />));
  await settle();
  await click(button('Load history'));
  await click(button('Compare baseline', row('A')));
  await click(button('Select other project'));
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  expect(container.querySelectorAll('[data-testid="history-row"]')).toHaveLength(0);
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
  expect(fixture.comparisonSignal?.aborted).toBe(true);
});
it('aborts and invalidates a comparison when its history filter changes', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  const input = container.querySelector<HTMLInputElement>(
    'section[aria-label="Research workspace"] input[inputmode="numeric"]',
  )!;
  expect(input.closest('label')!.textContent).toContain('Minimum frequency');
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '100');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
  expect(fixture.comparisonSignal?.aborted).toBe(true);
});
it('aborts the underlying comparison request when the component unmounts', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await act(async () => root!.unmount());
  root = undefined;
  expect(fixture.comparisonSignal?.aborted).toBe(true);
  fixture.comparison.resolve(comparison('A'));
  await settle();
});
it('clears same-project history/comparison on actual HWM rollback and accepts a fresh selection', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  await rollback(fixture);
  expect(container.querySelectorAll('[data-testid="history-row"]')).toHaveLength(0);
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
  await click(button('Load history'));
  expect(row('fresh').classList.contains('selected')).toBe(true);
  expect(fixture.requests.every((request) => request.body === undefined)).toBe(true);
});
it('aborts same-project pending history on HWM rollback and rejects its late old rows', async () => {
  const fixture = new CoreFixture();
  fixture.heldHistory = deferred();
  await mount(fixture);
  await click(button('Research'));
  await click(button('Load history'));
  await rollback(fixture);
  await act(async () =>
    fixture.heldHistory!.resolve({ items: [sweep('old-archive')], next_cursor: null }),
  );
  await settle();
  expect(container.querySelectorAll('[data-testid="history-row"]')).toHaveLength(0);
  expect(fixture.historySignal?.aborted).toBe(true);
});
it('clears event/original dialogs and rejects a late old original after same-project HWM rollback', async () => {
  const fixture = new CoreFixture();
  fixture.heldOriginal = deferred();
  await mount(fixture);
  await click(button('Details'));
  await click(button('Original sweep', container.querySelector('dialog')!));
  await rollback(fixture);
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
  await act(async () => fixture.heldOriginal!.resolve(sweep('old-original')));
  await settle();
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
  expect(fixture.originalSignal?.aborted).toBe(true);
});
it('does not start a new original lookup from a late pre-rollback event-source reply', async () => {
  const fixture = new CoreFixture();
  fixture.heldSources = deferred();
  await mount(fixture);
  await click(button('Details'));
  await click(button('Original sweep', container.querySelector('dialog')!));
  await rollback(fixture);
  await act(async () =>
    fixture.heldSources!.resolve({ items: [{ sweep_id: 'A' }], next_cursor: null }),
  );
  await settle();
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
  expect(fixture.requests.some((request) => request.path.endsWith('/history/A'))).toBe(false);
  expect(fixture.sourcesSignal?.aborted).toBe(true);
});
it('does not reopen an old event dialog when its pre-rollback metadata review acknowledges late', async () => {
  const fixture = new CoreFixture();
  fixture.heldReview = deferred();
  await mount(fixture);
  await click(button('Details'));
  const oldEvent = fixture.event();
  await click(button('Save review', container.querySelector('dialog')!));
  await rollback(fixture);
  await act(async () => fixture.heldReview!.resolve(oldEvent));
  await settle();
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
  expect(fixture.reviewSignal?.aborted).toBe(true);
});

it('does not publish a discarded comparison rejection into selected source B', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await click(row('B').querySelector<HTMLElement>('.history-select')!);
  await act(async () => fixture.comparison.reject(Error('Late discarded comparison A')));
  await settle();
  expect(action()).not.toContain('Late discarded comparison A');
  expect(action()).not.toContain('Compare baseline');
  expect(busy()).toBe(false);
});
it('does not confirm a discarded comparison acknowledgement for source B', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await click(row('B').querySelector<HTMLElement>('.history-select')!);
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  expect(action()).not.toContain('Compare baseline');
  expect(row('B').classList.contains('selected')).toBe(true);
});
it('retains a valid same-source comparison error and releases its busy state', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await act(async () => fixture.comparison.reject(Error('Current original comparison failed')));
  await settle();
  expect(action()).toContain('Error');
  expect(action()).toContain('Current original comparison failed');
  expect(busy()).toBe(false);
});
it('does not publish the old project read error after real useDesk project selection', async () => {
  const fixture = new CoreFixture();
  root = createRoot(container);
  await act(async () => root!.render(<ProjectHost transport={fixture.transport} />));
  await settle();
  await click(button('Load history'));
  await click(button('Compare baseline', row('A')));
  await click(button('Select other project'));
  await act(async () => fixture.comparison.reject(Error('Discarded old project comparison')));
  await settle();
  expect(action()).not.toContain('Discarded old project comparison');
  expect(action()).not.toContain('Compare baseline');
  expect(busy()).toBe(false);
});
it('keeps the archive-reset action when a pre-rollback comparison rejects late', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await rollback(fixture);
  const reset = action();
  expect(reset).toContain('Archive changed');
  await act(async () => fixture.comparison.reject(Error('Discarded replaced-archive comparison')));
  await settle();
  expect(action()).toBe(reset);
  expect(busy()).toBe(false);
});
it('releases only the discarded read and does not let its finally clear a new source action', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  const original = fixture.comparison;
  await click(button('Compare baseline', row('A')));
  await click(row('B').querySelector<HTMLElement>('.history-select')!);
  expect(busy()).toBe(false);
  fixture.comparison = deferred<Comparison>();
  await click(button('Compare baseline', row('B')));
  expect(fixture.requests.some((request) => request.path.endsWith('/history/B/comparison'))).toBe(
    true,
  );
  expect(busy()).toBe(true);
  const current = action();
  await act(async () => original.reject(Error('Old A finishes during B')));
  await settle();
  expect(action()).toBe(current);
  expect(busy()).toBe(true);
  await act(async () => fixture.comparison.resolve(comparison('B')));
  await settle();
  expect(action()).toContain('Confirmed');
  expect(busy()).toBe(false);
  expect(container.textContent).toContain('Baseline comparison · compatible');
});
it('does not start or label a comparison invalidated before the requested animation frame', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  const frames: FrameRequestCallback[] = [];
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  await click(button('Compare baseline', row('A')));
  await click(row('B').querySelector<HTMLElement>('.history-select')!);
  await act(async () => frames.splice(0).forEach((callback) => callback(0)));
  await settle();
  expect(fixture.requests.some((request) => request.path.endsWith('/comparison'))).toBe(false);
  expect(action()).not.toContain('Compare baseline');
  expect(busy()).toBe(false);
});
it('does not surface a closed original-evidence read rejection', async () => {
  const fixture = new CoreFixture();
  fixture.heldOriginal = deferred();
  await mount(fixture);
  await click(button('Details'));
  await click(button('Original sweep', container.querySelector('dialog')!));
  await click(button('Close event', container.querySelector('dialog')!));
  await act(async () => fixture.heldOriginal!.reject(Error('Closed original evidence failed')));
  await settle();
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
  expect(action()).not.toContain('Closed original evidence failed');
  expect(action()).not.toContain('Load original sweep');
  expect(busy()).toBe(false);
});
it('keeps a metadata mutation busy through its original late Core acknowledgement', async () => {
  const fixture = new CoreFixture();
  fixture.heldReview = deferred();
  await mount(fixture);
  await click(button('Details'));
  const original = fixture.event();
  await click(button('Save review', container.querySelector('dialog')!));
  await rollback(fixture);
  expect(busy()).toBe(true);
  await act(async () => fixture.heldReview!.resolve(original));
  await settle();
  expect(action()).toContain('Confirmed');
  expect(action()).toContain('Review event');
  expect(busy()).toBe(false);
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
});
it('preserves an explicit metadata command requested before an unmount animation frame', async () => {
  const fixture = new CoreFixture();
  fixture.heldReview = deferred();
  await mount(fixture);
  await click(button('Details'));
  const original = fixture.event();
  const frames: FrameRequestCallback[] = [];
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  await click(button('Save review', container.querySelector('dialog')!));
  await act(async () => root!.unmount());
  root = undefined;
  await act(async () => frames.splice(0).forEach((callback) => callback(0)));
  await settle();
  expect(
    fixture.requests.filter((request) => request.path.endsWith('/events/event/review')),
  ).toHaveLength(1);
  await act(async () => fixture.heldReview!.resolve(original));
  await settle();
});
it('does not surface a closed audio-evidence read rejection', async () => {
  const fixture = new CoreFixture();
  fixture.heldAudio = deferred<Blob>();
  await mount(fixture);
  await click(button('Details'));
  await click(button('Play audio', container.querySelector('dialog')!));
  expect(busy()).toBe(true);
  await click(button('Close event', container.querySelector('dialog')!));
  expect(fixture.audioSignal?.aborted).toBe(true);
  await act(async () => fixture.heldAudio!.reject(Error('Closed audio evidence failed')));
  await settle();
  expect(action()).not.toContain('Closed audio evidence failed');
  expect(action()).not.toContain('Load event audio');
  expect(busy()).toBe(false);
});
it('does not surface a discarded history read rejection after filter invalidation', async () => {
  const fixture = new CoreFixture();
  fixture.heldHistory = deferred();
  await mount(fixture);
  await click(button('Research'));
  await click(button('Load history'));
  const input = container.querySelector<HTMLInputElement>(
    'section[aria-label="Research workspace"] input[inputmode="numeric"]',
  )!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '100');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => fixture.heldHistory!.reject(Error('Discarded filtered history failed')));
  await settle();
  expect(fixture.historySignal?.aborted).toBe(true);
  expect(action()).not.toContain('Discarded filtered history failed');
  expect(action()).not.toContain('Load history');
  expect(busy()).toBe(false);
});

it('does not replay or reset evidence when a frame applies after its health request began', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Original', row('A')));
  expect(container.querySelector('dialog')).toBeTruthy();
  const held = deferred<Health>();
  fixture.healthReplies.push(held);
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  fixture.highWater = 101;
  let acknowledged: unknown;
  await act(async () => {
    acknowledged = await fixture.deliver(101);
  });
  await settle();
  expect(acknowledged).toBe(101);
  const subscriptions = fixture.cursors.length;
  await act(async () => held.resolve(health(100)));
  await settle();
  expect(fixture.cursors).toHaveLength(subscriptions);
  expect(row('A')).toBeTruthy();
  expect(container.querySelectorAll('dialog')).toHaveLength(1);
  expect(action()).not.toContain('Archive changed');
});
it('invalidates same-project evidence when an observed head goes backwards ahead of applied data', async () => {
  const fixture = new CoreFixture();
  fixture.highWater = 50;
  await load(fixture);
  await click(button('Original', row('A')));
  fixture.highWater = 100;
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  await settle();
  fixture.highWater = 99;
  fixture.restored = true;
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  await settle();
  expect(action()).toContain('Archive changed');
  expect(container.querySelectorAll('[data-testid="history-row"]')).toHaveLength(0);
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
});
it('ignores an older health response after a newer replay health response', async () => {
  const fixture = new CoreFixture();
  root = createRoot(container);
  await act(async () => root!.render(<ProjectHost transport={fixture.transport} />));
  await settle();
  const old = deferred<Health>();
  fixture.healthReplies.push(old);
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  fixture.highWater = 101;
  await click(button('Request replay'));
  expect(container.querySelector('[data-testid="health-cursor"]')!.textContent).toBe('101');
  await act(async () => old.resolve(health(100)));
  await settle();
  expect(container.querySelector('[data-testid="health-cursor"]')!.textContent).toBe('101');
  expect(container.querySelector('[data-testid="archive-generation"]')!.textContent).toBe('0');
});
it('ignores an old-node health response after an explicit connection epoch changes', async () => {
  const fixture = new CoreFixture();
  root = createRoot(container);
  await act(async () => root!.render(<ProjectHost transport={fixture.transport} />));
  await settle();
  const old = deferred<Health>();
  fixture.healthReplies.push(old);
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  fixture.connectHighWater = 10;
  await click(button('Connect another owned node'));
  expect(container.querySelector('[data-testid="health-cursor"]')!.textContent).toBe('10');
  await act(async () => old.resolve(health(100)));
  await settle();
  expect(container.querySelector('[data-testid="health-cursor"]')!.textContent).toBe('10');
});
it('retains observed-head identity with the existing applied checkpoint across a real remount', async () => {
  const fixture = new CoreFixture();
  fixture.highWater = 50;
  root = createRoot(container);
  await act(async () => root!.render(<ProjectHost transport={fixture.transport} />));
  await settle();
  fixture.highWater = 100;
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  await settle();
  expect(container.querySelector('[data-testid="live-cursor"]')!.textContent).toBe('50');
  await act(async () => root!.unmount());
  fixture.highWater = 99;
  fixture.restored = true;
  root = createRoot(container);
  await act(async () => root!.render(<ProjectHost transport={fixture.transport} />));
  await settle();
  expect(action()).toContain('Archive changed');
  expect(container.querySelector('[data-testid="live-cursor"]')!.textContent).toBe('99');
});
it('preserves exact large applied cursors when a newer committed frame overtakes health', async () => {
  const fixture = new CoreFixture();
  const before = 9007199254740992n;
  fixture.highWater = before;
  await load(fixture);
  const held = deferred<Health>();
  fixture.healthReplies.push(held);
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  fixture.highWater = before + 1n;
  let acknowledged: unknown;
  await act(async () => {
    acknowledged = await fixture.deliver(before + 1n);
  });
  expect(acknowledged).toBe(before + 1n);
  const subscriptions = fixture.cursors.length;
  await act(async () => held.resolve(health(before)));
  await settle();
  expect(fixture.cursors).toHaveLength(subscriptions);
  expect(row('A')).toBeTruthy();
  expect(action()).not.toContain('Archive changed');
});
it('ignores an old-node health failure after the new node has acknowledged health', async () => {
  const fixture = new CoreFixture();
  root = createRoot(container);
  await act(async () => root!.render(<ProjectHost transport={fixture.transport} />));
  await settle();
  const old = deferred<Health>();
  fixture.healthReplies.push(old);
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  fixture.connectHighWater = 10;
  await click(button('Connect another owned node'));
  await act(async () => old.reject(Error('Original node request failed late')));
  await settle();
  expect(container.querySelector('[data-testid="health-cursor"]')!.textContent).toBe('10');
});
it('retains a confirmed head rollback while the replacement archive grows before resync', async () => {
  const fixture = new CoreFixture();
  fixture.highWater = 50;
  await load(fixture);
  await click(button('Original', row('A')));
  fixture.highWater = 100;
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  const low = deferred<Health>();
  fixture.highWater = 99;
  fixture.restored = true;
  fixture.healthReplies.push(low);
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  fixture.highWater = 101;
  await act(async () => low.resolve(health(99)));
  await settle();
  expect(action()).toContain('Archive changed');
  expect(fixture.cursors.at(-1)).toBe(101);
  expect(container.querySelectorAll('[data-testid="history-row"]')).toHaveLength(0);
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
});
it('does not treat overlapping samples read in reverse order as an archive rollback', async () => {
  const fixture = new CoreFixture();
  root = createRoot(container);
  await act(async () => root!.render(<ProjectHost transport={fixture.transport} />));
  await settle();
  await click(button('Load history'));
  const olderRequest = deferred<Health>();
  fixture.healthReplies.push(olderRequest);
  await act(async () => vi.advanceTimersByTimeAsync(15000));
  const newerRequest = deferred<Health>();
  fixture.healthReplies.push(newerRequest);
  await click(button('Request replay'));
  // Both requests began after observed100. The older request reads105 later
  // than the newer request read104, then its response happens to arrive first.
  fixture.highWater = 105;
  await act(async () => olderRequest.resolve(health(105)));
  await settle();
  expect(container.querySelector('[data-testid="health-cursor"]')!.textContent).toBe('105');
  await act(async () => newerRequest.resolve(health(104)));
  await settle();
  expect(container.querySelector('[data-testid="health-cursor"]')!.textContent).toBe('105');
  expect(container.querySelector('[data-testid="archive-generation"]')!.textContent).toBe('0');
  expect(row('A')).toBeTruthy();
  expect(action()).not.toContain('Archive changed');
});

it('places research before live charts and preserves its state while switching local workspaces', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  const research = container.querySelector<HTMLElement>(
    'section[aria-label="Research workspace"]',
  )!;
  const live = container.querySelector<HTMLElement>('.live-grid')!;
  const status = container.querySelector<HTMLElement>('[data-testid="action-state"]')!;
  expect(research.hidden).toBe(false);
  expect(status.compareDocumentPosition(research) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(research.compareDocumentPosition(live) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const minimum = research.querySelector<HTMLInputElement>('input[inputmode="numeric"]')!;
  expect(minimum.closest('label')!.textContent).toContain('Minimum frequency');
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(
      minimum,
      '100000000',
    );
    minimum.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await click(row('B').querySelector<HTMLElement>('.history-select')!);
  const selected = row('B');
  const subscriptions = fixture.cursors.length;
  const historyReads = fixture.requests.filter((request) =>
    request.path.split('?')[0].endsWith('/history'),
  ).length;
  await click(button('Monitoring'));
  expect(research.hidden).toBe(true);
  expect(container.querySelector('section[aria-label="Research workspace"]')).toBe(research);
  expect(research.querySelector('input[inputmode="numeric"]')).toBe(minimum);
  expect(row('B')).toBe(selected);
  fixture.highWater = 101;
  await act(async () => {
    expect(await fixture.deliver(101)).toBe(101);
  });
  await settle();
  expect(container.querySelector('.live-grid')).toBe(live);
  expect(container.querySelector('[data-testid="source-id"]')!.textContent).toBe('committed-101');
  await click(button('Research'));
  expect(research.hidden).toBe(false);
  expect(container.querySelectorAll('section[aria-label="Research workspace"]')).toHaveLength(1);
  expect(minimum.value).toBe('100000000');
  expect(row('B')).toBe(selected);
  expect(selected.classList.contains('selected')).toBe(true);
  expect(fixture.cursors).toHaveLength(subscriptions);
  expect(
    fixture.requests.filter((request) => request.path.split('?')[0].endsWith('/history')),
  ).toHaveLength(historyReads);
});
