import { unwrap, type Transport } from './api';
import { subscribeEvents } from './sse';
import type { EventSequence } from './sequence';
import { responseValue, stringifyJSON } from './wire';
import {
  binaryStreamCapability,
  subscribeBinaryEvents,
  type BinaryStreamCapability,
} from './websocket';
import type { FrameInput } from './pipeline';
import type { Capabilities } from './types';

export interface Connection extends Transport {
  native: boolean;
  ready?: boolean;
  connect(node: string): Promise<void>;
  pair(code: string, label: string): Promise<void>;
  revokePairing(): Promise<void>;
  configureStream?(capabilities: Capabilities): void;
  subscribe(
    after: EventSequence,
    receive: (raw: FrameInput) => Promise<EventSequence | void> | void,
    state: (connected: boolean, replayReason?: string) => void,
  ): Promise<() => void>;
  audio(path: string, signal?: AbortSignal): Promise<Blob>;
  openLegacy(): Promise<void>;
}

async function limitedResponse(
  response: Response,
  maximum: number,
  signal?: AbortSignal,
): Promise<Uint8Array> {
  const reader = response.body?.getReader();
  if (!reader) throw Error('Empty core response');
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for (;;) {
      if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
      const next = await reader.read();
      if (next.done) break;
      length += next.value.length;
      if (length > maximum) throw Error('Core response exceeds client limit');
      chunks.push(next.value);
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  const result = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.length;
  }
  return result;
}

let browserBinary: BinaryStreamCapability | undefined;
export const browserConnection: Connection = {
  native: false,
  async connect() {},
  configureStream(capabilities) {
    browserBinary = binaryStreamCapability(capabilities);
  },
  async pair(code, label) {
    const result = unwrap<{ paired: boolean; credential: string; token?: unknown }>(
      await browserConnection.request('/api/v1/security/pair', {
        pairing_code: code,
        label,
        client: 'browser',
      }),
    );
    if (
      result.paired !== true ||
      result.credential !== 'browser_cookie' ||
      Object.hasOwn(result, 'token')
    )
      throw Error('Invalid browser pairing response');
  },
  async revokePairing() {
    unwrap(await browserConnection.request('/api/v1/security/credentials/current/revoke', {}));
  },
  async openLegacy() {
    location.assign('/');
  },
  async request(path, body, signal) {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) controller.abort();
    const timeout = setTimeout(cancel, 45000);
    try {
      const response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : stringifyJSON(body),
        credentials: 'same-origin',
        mode: 'same-origin',
        signal: controller.signal,
        cache: 'no-store',
        redirect: 'error',
      });
      const raw = await limitedResponse(response, 8_388_608, controller.signal);
      const value = responseValue(path, raw) as { error?: unknown };
      if (!response.ok && !value.error) throw Error(`Core HTTP ${response.status}`);
      return value;
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener('abort', cancel);
    }
  },
  async subscribe(after, receive, state) {
    const config = browserBinary;
    if (config)
      return subscribeBinaryEvents(after, async (bytes) => await receive(bytes), state, config, {
        disable: () => {
          if (browserBinary === config) browserBinary = undefined;
        },
        fallback: (cursor) => subscribeEvents(cursor, receive, state),
      });
    return subscribeEvents(after, receive, state);
  },
  async audio(path, signal) {
    const response = await fetch(path, { signal, cache: 'no-store', redirect: 'error' });
    if (!response.ok) throw Error(`Audio HTTP ${response.status}`);
    return new Blob(
      [(await limitedResponse(response, 33_554_432, signal)) as Uint8Array<ArrayBuffer>],
      { type: 'audio/wav' },
    );
  },
};

export async function connection(): Promise<Connection> {
  if (!('__TAURI_INTERNALS__' in window)) return browserConnection;
  const { invoke, Channel } = await import('@tauri-apps/api/core');
  let streamGeneration = 0;
  let binary: BinaryStreamCapability | undefined;
  return {
    native: true,
    configureStream(capabilities) {
      binary = binaryStreamCapability(capabilities);
    },
    async openLegacy() {
      await invoke('open_legacy');
    },
    async connect(node) {
      await invoke('connect_node', { node });
    },
    async pair(code, label) {
      await invoke('pair_node', { pairingCode: code, label });
    },
    async revokePairing() {
      await invoke('revoke_pairing');
    },
    async request(path, body, signal) {
      if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
      const requestId = crypto.randomUUID();
      const cancel = () => {
        void invoke('cancel_request', { requestId }).catch(() => {});
      };
      signal?.addEventListener('abort', cancel, { once: true });
      try {
        const response = await invoke<string>('api_request', {
          requestId,
          path,
          body: body === undefined ? null : stringifyJSON(body),
          audio: false,
        });
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        return responseValue(path, response);
      } finally {
        signal?.removeEventListener('abort', cancel);
      }
    },
    async subscribe(after, receive, state) {
      const generation = ++streamGeneration;
      const config = binary;
      const streamId = crypto.randomUUID();
      const channel = new Channel<{ kind: string; data?: string; delivery_id?: string }>();
      channel.onmessage = (message) => {
        if (generation !== streamGeneration) return;
        if ((message.kind === 'event' || message.kind === 'binary') && message.data) {
          void (async () => {
            try {
              if (!message.delivery_id) throw Error('Missing native delivery acknowledgement');
              if (message.kind === 'binary') {
                if (!config || message.data!.length > Math.ceil(config.maxFrameBytes / 3) * 4)
                  throw Error('Binary frame exceeds client limit');
                const bytes = Uint8Array.from(atob(message.data!), (char) => char.charCodeAt(0));
                if (bytes.byteLength > config.maxFrameBytes)
                  throw Error('Binary frame exceeds client limit');
                await receive(bytes.buffer);
              } else await receive(message.data!);
              if (generation === streamGeneration)
                await invoke('ack_event', { streamId, deliveryId: message.delivery_id });
            } catch (error) {
              if (
                message.kind === 'binary' &&
                (error as Error).name !== 'AbortError' &&
                binary === config
              )
                binary = undefined;
              if (generation === streamGeneration) {
                state(false, 'Native delivery interrupted — resynchronizing');
                void invoke('stop_events', { streamId }).catch(() => {});
              }
            }
          })();
        } else {
          if (message.kind === 'fallback' && binary === config) binary = undefined;
          state(message.kind === 'open');
        }
      };
      await invoke('start_events', {
        after: after.toString(),
        streamId,
        binary: config ? { max_frame_bytes: config.maxFrameBytes } : null,
        channel,
      });
      return () => {
        if (generation === streamGeneration) streamGeneration++;
        // A late start response still owns its stream. The native ID check makes
        // this cleanup safe even when a newer subscription has already started.
        void invoke('stop_events', { streamId }).catch(() => {});
      };
    },
    async audio(path, signal) {
      const requestId = crypto.randomUUID();
      const cancel = () => {
        void invoke('cancel_request', { requestId }).catch(() => {});
      };
      signal?.addEventListener('abort', cancel, { once: true });
      try {
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        const encoded = await invoke<string>('api_request', {
          requestId,
          path,
          body: null,
          audio: true,
        });
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        return new Blob([Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0))], {
          type: 'audio/wav',
        });
      } finally {
        signal?.removeEventListener('abort', cancel);
      }
    },
  };
}
