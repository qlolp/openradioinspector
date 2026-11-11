import { MAX_FRAME_BYTES } from '../../../contracts/generated/typescript/measurement_stream.ts';
import { eventSequence, type EventSequence } from './sequence';
import type { Capabilities } from './types';

export const BINARY_SUBPROTOCOL = 'ori.measurements.v1.protobuf';
export interface BinaryStreamCapability {
  path: '/api/v1/events/ws';
  subprotocol: typeof BINARY_SUBPROTOCOL;
  maxFrameBytes: number;
}
export function binaryStreamCapability(
  capabilities: Capabilities,
): BinaryStreamCapability | undefined {
  const features = capabilities?.features;
  if (!features || !Object.hasOwn(features, 'measurement_binary_stream')) return;
  const value = features.measurement_binary_stream as Record<string, unknown> | undefined;
  if (
    !value ||
    value.version !== 1 ||
    value.transport !== 'websocket' ||
    value.encoding !== 'protobuf' ||
    value.path !== '/api/v1/events/ws' ||
    value.subprotocol !== BINARY_SUBPROTOCOL ||
    typeof value.max_frame_bytes !== 'number' ||
    !Number.isSafeInteger(value.max_frame_bytes) ||
    value.max_frame_bytes < 1 ||
    value.max_frame_bytes > MAX_FRAME_BYTES
  )
    return;
  return {
    path: value.path,
    subprotocol: BINARY_SUBPROTOCOL,
    maxFrameBytes: value.max_frame_bytes,
  };
}

/** One server frame at a time; ACK is canonical decimal text after application. */
export function subscribeBinaryEvents(
  after: EventSequence,
  receive: (buffer: ArrayBuffer) => Promise<EventSequence | void>,
  state: (open: boolean) => void,
  config: BinaryStreamCapability,
  recovery: { fallback: (after: bigint) => () => void; disable: () => void },
): () => void {
  let cursor = BigInt(eventSequence(after, true)),
    stopped = false,
    attempt = 0;
  let socket: WebSocket | undefined,
    timer: ReturnType<typeof setTimeout> | undefined,
    fallbackCleanup: (() => void) | undefined;
  let disabled = false;
  const disable = () => {
    if (!disabled) {
      disabled = true;
      recovery.disable();
    }
  };
  const fallback = () => {
    if (stopped || fallbackCleanup) return;
    disable();
    state(false);
    fallbackCleanup = recovery.fallback(cursor);
  };
  const start = () => {
    if (stopped) return;
    const generation = ++attempt;
    let applying = false,
      ended = false,
      opened = false,
      bad = false,
      settled = false;
    const current = () => !stopped && generation === attempt;
    const settle = () => {
      if (!current() || !ended || applying || settled) return;
      settled = true;
      clearTimeout(timer);
      if (bad) fallback();
      else {
        state(false);
        timer = setTimeout(start, 1000);
      }
    };
    try {
      const url = new URL(config.path, location.href);
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      url.searchParams.set('after', cursor.toString());
      const connection = new WebSocket(url.href, config.subprotocol);
      socket = connection;
      connection.binaryType = 'arraybuffer';
      const fail = (code: number) => {
        bad = true;
        disable();
        // Browser APIs reserve protocol close codes 1001..2999 for the stack.
        // Application failures use 400x; malformed ACKs remain server 1008/1009.
        try {
          connection.close(code === 1000 ? 1000 : code + 3000);
        } catch {
          ended = true;
          settle();
        }
      };
      timer = setTimeout(() => {
        if (current() && !opened) connection.close();
      }, 10000);
      connection.onopen = () => {
        if (!current()) return;
        clearTimeout(timer);
        if (connection.protocol !== config.subprotocol) {
          fail(1002);
          return;
        }
        opened = true;
        state(true);
      };
      connection.onmessage = (event: MessageEvent) => {
        if (!current() || ended || bad) return;
        if (applying) {
          fail(1008);
          return;
        }
        if (!(event.data instanceof ArrayBuffer)) {
          fail(1003);
          return;
        }
        if (event.data.byteLength > config.maxFrameBytes) {
          fail(1009);
          return;
        }
        applying = true;
        void (async () => {
          try {
            const applied = BigInt(eventSequence(await receive(event.data)));
            if (!current()) return;
            if (applied > cursor) cursor = applied;
            if (!bad && connection.readyState === 1) {
              try {
                connection.send(applied.toString());
              } catch {
                connection.close();
              }
            }
          } catch (error) {
            if ((error as Error).name !== 'AbortError') {
              // LiveStore may already have cancelled this subscription after a
              // parser failure. Disable this negotiated mode for its next retry.
              disable();
              bad = true;
              if (current()) fail(1007);
            }
          } finally {
            applying = false;
            settle();
          }
        })();
      };
      connection.onclose = () => {
        if (!current()) return;
        ended = true;
        clearTimeout(timer);
        settle();
      };
      connection.onerror = () => {
        if (!current()) return;
        try {
          connection.close();
        } catch {
          ended = true;
          settle();
        }
      };
    } catch {
      if (typeof WebSocket === 'undefined') fallback();
      else {
        state(false);
        timer = setTimeout(start, 1000);
      }
    }
  };
  start();
  return () => {
    stopped = true;
    attempt++;
    clearTimeout(timer);
    fallbackCleanup?.();
    try {
      socket?.close();
    } catch {
      /* Already closed. */
    }
  };
}
