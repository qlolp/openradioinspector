import { afterEach, expect, it, vi } from 'vitest';
import { SseDecoder } from '../src/sse';
import { browserConnection } from '../src/transport';
afterEach(() => vi.unstubAllGlobals());
it('decodes all named events across split UTF8/CRLF and multiline data with a byte bound', () => {
  const decoder = new SseDecoder(),
    text =
      ': heartbeat\r\nid: 9007199254740993\r\nevent: future.unlisted\r\ndata: {"note":"Ж𐀀",\r\ndata: "sequence":9007199254740993}\r\n\r\n';
  const events = [];
  for (const byte of new TextEncoder().encode(text))
    events.push(...decoder.push(new Uint8Array([byte])));
  expect(events).toEqual([
    { id: '9007199254740993', data: '{"note":"Ж𐀀",\n"sequence":9007199254740993}' },
  ]);
  expect(() => decoder.push(new Uint8Array(8_388_609))).toThrow(/limit/i);
});
it('delivers unlisted audio and future event types and reconnects with exact cursor/cookie policy', async () => {
  const received: string[] = [],
    calls: { url: string; options: RequestInit }[] = [];
  let second: ReadableStreamDefaultController<Uint8Array> | undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options: RequestInit) => {
      calls.push({ url, options });
      if (calls.length === 1)
        return new Response(
          'id: 9007199254740993\nevent: audio_segment.received\ndata: {"data":{"sequence":9007199254740993}}\n\nid: 9007199254740994\nevent: future.unlisted\ndata: {"data":{"sequence":9007199254740994}}\n\n',
          { headers: { 'Content-Type': 'text/event-stream' } },
        );
      return new Response(
        new ReadableStream<Uint8Array>({
          start(c) {
            second = c;
          },
        }),
        { headers: { 'Content-Type': 'text/event-stream' } },
      );
    }),
  );
  const stop = await browserConnection.subscribe(
    9007199254740992n,
    (raw) => {
      if (typeof raw !== 'string') throw Error('Expected SSE text');
      received.push(raw);
    },
    () => {},
  );
  await vi.waitFor(() => expect(calls).toHaveLength(2), { timeout: 2500 });
  expect(received).toHaveLength(2);
  expect(calls[1].url).toContain('after=9007199254740994');
  expect(calls[1].options.credentials).toBe('same-origin');
  expect(calls[1].options.redirect).toBe('error');
  stop();
  expect(calls[1].options.signal?.aborted).toBe(true);
  second?.close();
});

it('awaits application before delivering the next frame or advancing the reconnect cursor', async () => {
  const calls: string[] = [],
    received: string[] = [];
  let apply!: () => void;
  const blocked = new Promise<void>((resolve) => {
    apply = resolve;
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      calls.push(url);
      if (calls.length === 1)
        return new Response(
          'id: 9007199254740993\ndata: first\n\nid: 9007199254740994\ndata: second\n\n',
          { headers: { 'Content-Type': 'text/event-stream' } },
        );
      return new Response(new ReadableStream(), {
        headers: { 'Content-Type': 'text/event-stream' },
      });
    }),
  );
  const stop = await browserConnection.subscribe(
    9007199254740992n,
    async (raw) => {
      if (typeof raw !== 'string') throw Error('Expected SSE text');
      received.push(raw);
      if (raw === 'first') await blocked;
      else throw Error('Parser failed before applying second');
    },
    () => {},
  );
  await vi.waitFor(() => expect(received).toEqual(['first']));
  expect(calls).toHaveLength(1);
  apply();
  await vi.waitFor(() => expect(calls).toHaveLength(2), { timeout: 2500 });
  expect(calls[1]).toContain('after=9007199254740993');
  stop();
});

it('cancels while application is blocked without delivering queued frames or reconnecting', async () => {
  let unblock!: () => void;
  const blocked = new Promise<void>((resolve) => {
    unblock = resolve;
  });
  const received: string[] = [];
  const fetcher = vi.fn(
    async () =>
      new Response('id: 1\ndata: first\n\nid: 2\ndata: second\n\n', {
        headers: { 'Content-Type': 'text/event-stream' },
      }),
  );
  vi.stubGlobal('fetch', fetcher);
  const stop = await browserConnection.subscribe(
    0,
    async (raw) => {
      if (typeof raw !== 'string') throw Error('Expected SSE text');
      received.push(raw);
      await blocked;
    },
    () => {},
  );
  await vi.waitFor(() => expect(received).toEqual(['first']));
  stop();
  unblock();
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(received).toEqual(['first']);
  expect(fetcher).toHaveBeenCalledTimes(1);
});
