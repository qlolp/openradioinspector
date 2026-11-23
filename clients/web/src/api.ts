import { encodePathSegment } from './wire';
import { rfExplorerSession } from './rf-explorer';
import { gridPointCount } from './scan-plan';
import type { Stream } from './types';
export interface Transport {
  request(path: string, body?: unknown, signal?: AbortSignal): Promise<unknown>;
}
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
  ) {
    super(message.slice(0, 512));
    this.name = 'ApiError';
  }
}
export function unwrap<T>(value: unknown): T {
  const envelope = value as {
    contract?: string;
    contract_version?: number;
    data?: T;
    error?: { code?: string; message?: string };
  };
  if (envelope.error)
    throw new ApiError(
      envelope.error.code || 'request_failed',
      envelope.error.message || 'Core request failed',
    );
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
    const route = '/monitoring-projects/' + encodePathSegment(id) + (path ? '/' + path : '');
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
export type DutySource = 'tinysa' | 'rf_explorer' | 'ardv1';

export interface ReceiveForm {
  name: string;
  simulator: boolean;
  analyzerPort: string;
  receiverPort: string;
  monitoringReadback?: boolean;
  primarySource?: DutySource;
  checkEvents?: boolean;
  tinyGrid?: boolean;
  stepHz?: number;
  startHz: number;
  stopHz: number;
  points: number;
  rbwHz: number;
}

export function receiverGridCount(start: number, stop: number, step: number) {
  if (
    ![start, stop, step].every((value) => Number.isSafeInteger(value) && value % 10 === 0) ||
    start < 100_000 ||
    stop > 1_300_000_000 ||
    stop <= start ||
    step < 10 ||
    step > 1_300_000_000
  )
    throw Error(
      'AR-DV1 requires increasing endpoints within 0.1–1300 MHz and a step in multiples of 0.01 kHz.',
    );
  const count = Math.ceil((stop - start) / step) + 1;
  if (count > 129_991)
    throw Error('This AR-DV1 plan exceeds 129,991 points. Increase the step or shorten the range.');
  return count;
}

export function tinyDutyPlan(form: ReceiveForm) {
  const points = gridPointCount(String(form.startHz), String(form.stopHz));
  if (!points || form.stepHz !== 10_000 || ![10_000, 30_000].includes(form.rbwHz))
    throw Error(
      'tinySA requires a whole number of 10 kHz steps within 0.1–1300 MHz and 10 or 30 kHz RBW.',
    );
  return {
    name: form.name,
    bands: [
      {
        id: 'duty',
        name: form.name,
        start_hz: form.startHz,
        stop_hz: form.stopHz,
        points,
        step_hz: 10_000,
        rbw_hz: form.rbwHz,
        max_revisit_seconds: 3600,
      },
    ],
  };
}

export function receiveSession(form: ReceiveForm) {
  const { startHz, stopHz, rbwHz, primarySource = 'tinysa' } = form;
  const receiverOnly = primarySource === 'ardv1';
  const checkEvents = form.checkEvents ?? !form.primarySource;
  const ports = [
    ...(!receiverOnly ? [form.analyzerPort.trim()] : []),
    ...(receiverOnly || checkEvents ? [form.receiverPort.trim()] : []),
  ];
  if (receiverOnly && checkEvents)
    throw Error('A single AR-DV1 must pause its panorama to check an event.');
  if (!form.simulator && ports.some((port) => !port || port.includes('://') || port.includes('\0')))
    throw Error('Enter a physical serial port on the selected node for each instrument.');
  const portIds = ports.map((port) => (/^com\d+$/i.test(port) ? port.toUpperCase() : port));
  if (!form.simulator && new Set(portIds).size !== ports.length)
    throw Error('Two distinct explicit serial ports are required');
  if (form.simulator && primarySource !== 'tinysa')
    throw Error('This source requires its physical serial profile.');
  const streams: (Omit<Stream, 'id'> & { capabilities: string[] })[] = [];
  if (primarySource === 'rf_explorer') {
    if (form.stepHz !== 10_000)
      throw Error(
        'RF Explorer survey uses 10 kHz spacing. Select 10 kHz or change the primary source.',
      );
    streams.push(
      ...rfExplorerSession(form.name, form.analyzerPort, {
        start: String(startHz),
        stop: String(stopHz),
        survey: true,
      }).streams,
    );
  } else if (!receiverOnly) {
    let high = stopHz,
      points = form.points;
    const gridEnabled =
      form.tinyGrid ?? (form.primarySource === 'tinysa' && form.stepHz === 10_000);
    if (!gridEnabled && form.primarySource === 'tinysa' && form.stepHz !== undefined) {
      points = (stopHz - startHz) / form.stepHz + 1;
      if (
        !Number.isSafeInteger(form.stepHz) ||
        form.stepHz < 1 ||
        !Number.isSafeInteger(points) ||
        points < 2 ||
        points > 290 ||
        stopHz > 800_000_000
      )
        throw Error(
          'For this tinySA step choose 2–290 equally spaced points up to 800 MHz. A 10 kHz survey supports longer ranges.',
        );
    }
    if (gridEnabled) {
      const grid = tinyDutyPlan(form).bands[0];
      points = Math.min(grid.points, 290);
      if (grid.points === 291) points--;
      high = startHz + (points - 1) * grid.step_hz;
    } else if (
      ![startHz, stopHz, points, rbwHz].every(Number.isSafeInteger) ||
      startHz < 100_000 ||
      stopHz > 800_000_000 ||
      stopHz <= startHz ||
      points < 2 ||
      points > 290 ||
      ![10000, 30000].includes(rbwHz)
    )
      throw Error('Choose a conservative receive range, 2–290 points and 10/30 kHz RBW');
    streams.push({
      device: 'tinySA Ultra',
      role: 'spectrum_analyzer',
      level_unit: 'dBm',
      port: form.simulator ? 'sim://tinysa' : form.analyzerPort.trim(),
      capabilities: ['sweep', 'waterfall'],
      settings: {
        adapter: form.simulator ? 'tinysa-sim' : 'tinysa-serial',
        start_hz: startHz,
        stop_hz: high,
        points,
        rbw_hz: rbwHz,
        period_s: 1,
        receive_profile: 'tinysa-ultra-zs405-receive-v1',
      },
    });
  }
  if (receiverOnly || checkEvents) {
    const step = form.stepHz ?? 10_000;
    if (receiverOnly) receiverGridCount(startHz, stopHz, step);
    streams.push({
      device: 'AR-DV1',
      role: 'receiver',
      level_unit: 's_meter_count',
      port: form.simulator ? 'sim://ardv1' : form.receiverPort.trim(),
      capabilities: ['tune'],
      settings: {
        adapter: form.simulator ? 'ardv1-sim' : 'ardv1-serial',
        frequency_hz: receiverOnly ? startHz : 100_000_000,
        mode: 'FM',
        receiver_profile: 'ardv1-monitor-v1',
        period_s: receiverOnly ? 0 : 1,
        ...(receiverOnly && {
          fast_scan: {
            start_hz: startHz,
            stop_hz: stopHz,
            step_hz: step,
            settle_s: 0.015,
            strategy: 'auto',
            cyclic: true,
          },
        }),
        ...(!form.simulator && form.monitoringReadback ? { monitoring_rx_readback: true } : {}),
      },
    });
  }
  return {
    name: form.name,
    streams,
    metadata: { receive_only: true, hardware_validation: 'not_run' },
  };
}
