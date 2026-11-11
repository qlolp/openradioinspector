export interface Transport {
  request(path: string, body?: unknown, signal?: AbortSignal): Promise<unknown>;
}
export function unwrap<T>(value: unknown): T {
  const envelope = value as {
    contract?: string;
    contract_version?: number;
    data?: T;
    error?: { message?: string };
  };
  if (envelope.error) throw Error(envelope.error.message || 'Core request failed');
  if (
    envelope.contract !== 'openradioinspector-platform' ||
    envelope.contract_version !== 1 ||
    !('data' in envelope)
  )
    throw Error('Unsupported API contract');
  return envelope.data as T;
}
export class Api {
  constructor(private transport: Transport) {}
  async get<T = unknown>(path: string, signal?: AbortSignal): Promise<T> {
    return unwrap<T>(await this.transport.request('/api/v1' + path, undefined, signal));
  }
  async post<T = unknown>(path: string, body: unknown = {}, signal?: AbortSignal): Promise<T> {
    return unwrap<T>(await this.transport.request('/api/v1' + path, body, signal));
  }
  async project<T = unknown>(
    id: string,
    path = '',
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> {
    const route = '/monitoring-projects/' + encodeURIComponent(id) + (path ? '/' + path : '');
    return body === undefined ? this.get<T>(route, signal) : this.post<T>(route, body, signal);
  }
}
export class ResourceScope {
  private generation = 0;
  private controller = new AbortController();
  next() {
    this.controller.abort();
    this.controller = new AbortController();
    return { generation: ++this.generation, signal: this.controller.signal };
  }
  current(generation: number) {
    return generation === this.generation && !this.controller.signal.aborted;
  }
  dispose() {
    this.controller.abort();
    this.generation++;
  }
}
export interface HistoryFilters {
  start?: string;
  end?: string;
  min_hz?: string;
  max_hz?: string;
  event_id?: string;
}
export function historyQuery(filters: HistoryFilters, cursor?: string): string {
  const query = new URLSearchParams({ limit: '32' });
  for (const key of ['start', 'end', 'min_hz', 'max_hz', 'event_id'] as const) {
    let value = filters[key];
    if (!value) continue;
    if (key === 'start' || key === 'end') {
      const date = new Date(value.endsWith('Z') ? value : value + 'Z');
      if (!Number.isFinite(date.valueOf())) throw Error('Invalid UTC time');
      value = date.toISOString();
    } else if (key === 'min_hz' || key === 'max_hz') {
      if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)))
        throw Error('Frequency must be integer Hz');
    }
    query.set(key, value);
  }
  if (cursor) query.set('after', cursor);
  return '?' + query;
}
export interface ReceiveForm {
  name: string;
  simulator: boolean;
  analyzerPort: string;
  receiverPort: string;
  startHz: number;
  stopHz: number;
  points: number;
  rbwHz: number;
}
export function receiveSession(form: ReceiveForm) {
  const { startHz, stopHz, points, rbwHz } = form;
  if (
    ![startHz, stopHz, points, rbwHz].every(Number.isSafeInteger) ||
    startHz < 100_000 ||
    stopHz > 800_000_000 ||
    stopHz <= startHz ||
    points < 2 ||
    points > 290 ||
    ![10000, 30000].includes(rbwHz)
  )
    throw Error('Choose a conservative receive range, 2–290 points and 10/30 kHz RBW');
  if (
    !form.simulator &&
    (!form.analyzerPort.trim() ||
      !form.receiverPort.trim() ||
      form.analyzerPort === form.receiverPort)
  )
    throw Error('Two distinct explicit serial ports are required');
  return {
    name: form.name,
    streams: [
      {
        device: 'tinySA Ultra',
        role: 'spectrum_analyzer',
        level_unit: 'dBm',
        port: form.simulator ? 'sim://tinysa' : form.analyzerPort,
        capabilities: ['sweep', 'waterfall'],
        settings: {
          adapter: form.simulator ? 'tinysa-sim' : 'tinysa-serial',
          start_hz: startHz,
          stop_hz: stopHz,
          points,
          rbw_hz: rbwHz,
          period_s: 1,
          receive_profile: 'tinysa-ultra-zs405-receive-v1',
        },
      },
      {
        device: 'AR-DV1',
        role: 'receiver',
        level_unit: 's_meter_count',
        port: form.simulator ? 'sim://ardv1' : form.receiverPort,
        capabilities: ['tune'],
        settings: {
          adapter: form.simulator ? 'ardv1-sim' : 'ardv1-serial',
          frequency_hz: 100_000_000,
          mode: 'FM',
          receiver_profile: 'ardv1-monitor-v1',
          period_s: 1,
        },
      },
    ],
    metadata: { receive_only: true, hardware_validation: 'not_run' },
  };
}
