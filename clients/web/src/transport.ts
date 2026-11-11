import type { Transport } from './api';
import { EVENT_TYPES } from './pipeline';

export interface Connection extends Transport {
  native: boolean;
  connect(node: string, token: string): Promise<void>;
  subscribe(
    after: number,
    receive: (raw: string) => void,
    state: (connected: boolean) => void,
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

export const browserConnection: Connection = {
  native: false,
  async connect() {},
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
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        cache: 'no-store',
        redirect: 'error',
      });
      const raw = await limitedResponse(response, 8_388_608, controller.signal);
      const value = JSON.parse(new TextDecoder().decode(raw));
      if (!response.ok && !value.error) throw Error(`Core HTTP ${response.status}`);
      return value;
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener('abort', cancel);
    }
  },
  async subscribe(after, receive, state) {
    const source = new EventSource('/api/v1/events?after=' + after);
    source.onopen = () => state(true);
    source.onerror = () => state(false);
    const listener = (event: Event) => receive((event as MessageEvent<string>).data);
    source.onmessage = listener;
    for (const name of EVENT_TYPES) source.addEventListener(name, listener);
    return () => source.close();
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
  return {
    native: true,
    async openLegacy() {
      await invoke('open_legacy');
    },
    async connect(node, token) {
      await invoke('connect_node', { node, token });
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
          body: body ?? null,
          audio: false,
        });
        if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
        return JSON.parse(response);
      } finally {
        signal?.removeEventListener('abort', cancel);
      }
    },
    async subscribe(after, receive, state) {
      const generation = ++streamGeneration;
      const channel = new Channel<{ kind: string; data?: string }>();
      channel.onmessage = (message) => {
        if (generation !== streamGeneration) return;
        if (message.kind === 'event' && message.data) receive(message.data);
        else state(message.kind === 'open');
      };
      await invoke('start_events', { after, channel });
      return () => {
        if (generation === streamGeneration) {
          streamGeneration++;
          void invoke('stop_events').catch(() => {});
        }
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
