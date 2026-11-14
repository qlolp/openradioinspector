import { eventSequence, type EventSequence } from './sequence';
export interface SseFrame {
  id?: string;
  data: string;
}
const MAX_FRAME = 8_388_608;
/** Event names are intentionally not filtered. The canonical sequence spans every type. */
export class SseDecoder {
  private utf8 = new TextDecoder('utf-8', { fatal: true });
  private encoder = new TextEncoder();
  private pending = '';
  private data: string[] = [];
  private id?: string;
  private bytes = 0;
  push(input: Uint8Array): SseFrame[] {
    if (input.byteLength > MAX_FRAME) throw Error('SSE frame exceeds client limit');
    this.pending += this.utf8.decode(input, { stream: true });
    const output: SseFrame[] = [];
    for (;;) {
      const end = this.pending.indexOf('\n');
      if (end < 0) break;
      const raw = this.pending.slice(0, end);
      this.pending = this.pending.slice(end + 1);
      this.bytes += this.encoder.encode(raw).length + 1;
      if (this.bytes > MAX_FRAME) throw Error('SSE frame exceeds client limit');
      const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
      if (!line) {
        if (this.data.length)
          output.push({
            ...(this.id === undefined ? {} : { id: this.id }),
            data: this.data.join('\n'),
          });
        this.data = [];
        this.id = undefined;
        this.bytes = 0;
      } else if (!line.startsWith(':')) {
        const colon = line.indexOf(':');
        const field = colon < 0 ? line : line.slice(0, colon);
        let value = colon < 0 ? '' : line.slice(colon + 1);
        if (value.startsWith(' ')) value = value.slice(1);
        if (field === 'data') this.data.push(value);
        else if (field === 'id' && !value.includes('\0')) this.id = value;
      }
    }
    if (this.bytes + this.encoder.encode(this.pending).length > MAX_FRAME)
      throw Error('SSE frame exceeds client limit');
    return output;
  }
  finish(): void {
    this.utf8.decode();
  }
}
function delay(signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    };
    const timer = setTimeout(done, 1000);
    signal.addEventListener('abort', done, { once: true });
    if (signal.aborted) done();
  });
}
export function subscribeEvents(
  after: EventSequence,
  receive: (raw: string) => Promise<EventSequence | void> | void,
  state: (open: boolean) => void,
): () => void {
  let cursor = BigInt(eventSequence(after, true));
  const lifetime = new AbortController();
  void (async () => {
    while (!lifetime.signal.aborted) {
      const attempt = new AbortController();
      const cancel = () => attempt.abort();
      lifetime.signal.addEventListener('abort', cancel, { once: true });
      let watchdog: ReturnType<typeof setTimeout> | undefined;
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
      const resetTimeout = () => {
        clearTimeout(watchdog);
        watchdog = setTimeout(cancel, 30000);
      };
      try {
        resetTimeout();
        const response = await fetch('/api/v1/events?after=' + cursor.toString(), {
          signal: attempt.signal,
          headers: { Accept: 'text/event-stream' },
          credentials: 'same-origin',
          mode: 'same-origin',
          redirect: 'error',
          cache: 'no-store',
        });
        if (!response.ok || !response.headers.get('Content-Type')?.startsWith('text/event-stream'))
          throw Error('SSE unavailable');
        reader = response.body?.getReader();
        if (!reader) throw Error('Empty SSE response');
        const decoder = new SseDecoder();
        state(true);
        while (!lifetime.signal.aborted) {
          const chunk = await reader.read();
          if (chunk.done) {
            decoder.finish();
            break;
          }
          resetTimeout();
          for (const frame of decoder.push(chunk.value)) {
            if (lifetime.signal.aborted) break;
            const sequence =
              frame.id === undefined
                ? undefined
                : BigInt(
                    eventSequence(
                      /^(0|[1-9][0-9]*)$/.test(frame.id) ? BigInt(frame.id) : undefined,
                    ),
                  );
            // Flow control extends through the worker and canonical sequence gate. Never
            // consume an unbounded replay or acknowledge data merely queued for parsing.
            clearTimeout(watchdog);
            await receive(frame.data);
            if (lifetime.signal.aborted) break;
            if (sequence !== undefined && sequence > cursor) cursor = sequence;
            resetTimeout();
          }
        }
      } catch {
        /* A partial frame is discarded and replayed from the last complete delivered ID. */
      } finally {
        clearTimeout(watchdog);
        attempt.abort();
        lifetime.signal.removeEventListener('abort', cancel);
        await reader?.cancel().catch(() => {});
      }
      if (!lifetime.signal.aborted) {
        state(false);
        await delay(lifetime.signal);
      }
    }
  })();
  return () => lifetime.abort();
}
