import { decodeFrame } from '../../../contracts/generated/typescript/measurement_stream.ts';
import type { DisplaySweep, Observation, Sweep } from './types';
import { observationValue, parseWire, sweepValue } from './wire';
import type { JSONObject } from '../../../contracts/generated/typescript/index.ts';
import { eventSequence, type EventSequence } from './sequence';

export type FrameInput = string | ArrayBuffer;
export interface UnsupportedSweep {
  id: string;
  session_id: string;
  stream_id: string;
  interval: Sweep['interval'];
  frequency_hz: bigint;
  point_count: number;
  level_unit: string;
}
type RenderSource = Omit<Sweep, 'frequencies_hz' | 'levels'> & {
  frequencies_hz: (number | bigint)[] | BigUint64Array;
  levels: number[] | Float64Array;
};
// Rendering copies only; never modify canonical source arrays or metadata.
export function prepareSweep(
  sweep: RenderSource,
  limit = 2048,
  typed = false,
): { sweep?: DisplaySweep; unsupported?: UnsupportedSweep } {
  const { frequencies_hz: hz, levels } = sweep;
  if (
    !(Array.isArray(hz) || hz instanceof BigUint64Array) ||
    !(Array.isArray(levels) || levels instanceof Float64Array) ||
    !levels.length ||
    hz.length !== levels.length ||
    levels.length > 1_000_000 ||
    !Number.isInteger(limit) ||
    limit < 3
  )
    throw Error('Invalid sweep dimensions');
  let peak = 0,
    outside: bigint | undefined;
  for (let i = 0; i < levels.length; i++) {
    const frequency = hz[i];
    if (
      !Number.isFinite(levels[i]) ||
      !(typeof frequency === 'bigint'
        ? frequency >= 0n
        : Number.isSafeInteger(frequency) && frequency >= 0) ||
      (i > 0 && frequency < hz[i - 1])
    )
      throw Error('Invalid sweep values');
    if (typeof frequency === 'bigint' && frequency > BigInt(Number.MAX_SAFE_INTEGER))
      outside ??= frequency;
    if (levels[i] > levels[peak]) peak = i;
  }
  if (outside !== undefined)
    return {
      unsupported: {
        id: sweep.id,
        session_id: sweep.session_id,
        stream_id: sweep.stream_id,
        interval: { started_at: sweep.interval.started_at, ended_at: sweep.interval.ended_at },
        frequency_hz: outside,
        point_count: levels.length,
        level_unit: sweep.level_unit,
      },
    };
  let indices: number[];
  if (hz.length <= limit) indices = Array.from({ length: hz.length }, (_, i) => i);
  else {
    const chosen = new Set([0, hz.length - 1, peak]),
      buckets = limit - 3;
    for (let b = 0; b < buckets; b++) {
      const first = Math.floor((b * hz.length) / buckets),
        end = Math.floor(((b + 1) * hz.length) / buckets);
      let best = first;
      for (let i = first + 1; i < end; i++) if (levels[i] > levels[best]) best = i;
      chosen.add(best);
    }
    indices = [...chosen].sort((a, b) => a - b);
  }
  // The full scan proved these Hz are exact safe integers before Number conversion.
  const frequencies = indices.map((i) => Number(hz[i])),
    selectedLevels = indices.map((i) => levels[i]);
  return {
    sweep: {
      id: sweep.id,
      session_id: sweep.session_id,
      stream_id: sweep.stream_id,
      interval: { started_at: sweep.interval.started_at, ended_at: sweep.interval.ended_at },
      level_unit: sweep.level_unit,
      frequencies_hz: typed ? new Float64Array(frequencies) : frequencies,
      levels: typed ? new Float64Array(selectedLevels) : selectedLevels,
      source_indices: typed ? new Uint32Array(indices) : indices,
      original_point_count:
        typeof sweep.original_point_count === 'number' &&
        Number.isSafeInteger(sweep.original_point_count) &&
        sweep.original_point_count >= levels.length
          ? sweep.original_point_count
          : levels.length,
      display_only: true,
      ...(typeof sweep.settings?.scan_plan_id === 'string' &&
      sweep.settings.scan_plan_id.length <= 200 &&
      typeof sweep.settings?.scan_band_id === 'string' &&
      /^[A-Za-z0-9_-]{1,64}$/.test(sweep.settings.scan_band_id)
        ? {
            scan_context: {
              plan_id: sweep.settings.scan_plan_id,
              band_id: sweep.settings.scan_band_id,
            },
          }
        : {}),
      peak: { index: peak, frequency_hz: Number(hz[peak]), level: levels[peak] },
    },
  };
}
export function decimate(sweep: Sweep, limit = 2048): DisplaySweep {
  const prepared = prepareSweep(sweep, limit);
  if (!prepared.sweep) throw Error('Source frequency outside supported display range');
  return prepared.sweep;
}

export interface Frame {
  sequence: EventSequence;
  event_type: string;
  session_id?: string;
  sweep?: DisplaySweep;
  unsupported?: UnsupportedSweep;
  observation?: Observation;
  invalidations: string[];
}
export const EVENT_TYPES = [
  'session.created',
  'session.started',
  'session.stopped',
  'device.state',
  'device.error',
  'device.tuned',
  'sweep.received',
  'observation.received',
  'candidate.created',
  'candidate.dismissed',
  'investigation.queued',
  'investigation.tuning',
  'investigation.listening',
  'investigation.completed',
  'investigation.failed',
  'investigation.cancelled',
  'investigation.interrupted',
  'automatic_handoff.policy_changed',
  'automatic_handoff.started',
  'automatic_handoff.disabled',
  'automatic_detection.policy_changed',
  'automatic_detection.candidate_created',
];
export function parseFrame(raw: FrameInput): Frame {
  if (typeof raw === 'string' && raw.length > 8_388_608) throw Error('Event exceeds client limit');
  const decoded = typeof raw === 'string' ? undefined : decodeFrame(new Uint8Array(raw));
  const envelope = (decoded ? decoded.envelope : parseWire(raw as string)) as JSONObject;
  if (envelope.contract !== 'openradioinspector-platform' || envelope.contract_version !== 1)
    throw Error('Unsupported event contract');
  const event = envelope.data as JSONObject;
  const sequence = decoded?.sequence ?? eventSequence(event?.sequence);
  if (typeof event.event_type !== 'string') throw Error('Invalid event type');
  const frame: Frame = {
    sequence,
    event_type: event.event_type,
    session_id: event.session_id as string | undefined,
    invalidations: [],
  };
  if (event.event_type === 'sweep.received') {
    const rawSweep = (event.payload as JSONObject).sweep;
    if (decoded?.spectrum) {
      // Validate known scalar/header fields with an actual first source point.
      // Packed arrays stay typed and are never materialized as JSON arrays.
      const arrays = decoded.spectrum;
      const header = sweepValue({
        ...(rawSweep as JSONObject),
        frequencies_hz: [arrays.frequenciesHz[0]],
        levels: [arrays.levels[0]],
      });
      Object.assign(
        frame,
        prepareSweep(
          { ...header, frequencies_hz: arrays.frequenciesHz, levels: arrays.levels },
          2048,
          true,
        ),
      );
    } else Object.assign(frame, prepareSweep(sweepValue(rawSweep), 2048, decoded !== undefined));
  } else if (event.event_type === 'observation.received') {
    const o = observationValue((event.payload as JSONObject).observation);
    if (
      !o?.id ||
      !o.interval?.started_at ||
      !(typeof o.frequency_hz === 'bigint'
        ? o.frequency_hz > 0n
        : Number.isSafeInteger(o.frequency_hz) && o.frequency_hz > 0) ||
      !o.level_unit ||
      (o.level != null && !Number.isFinite(o.level))
    )
      throw Error('Invalid receiver observation');
    frame.observation = {
      id: o.id,
      session_id: o.session_id,
      stream_id: o.stream_id,
      interval: { started_at: o.interval.started_at, ended_at: o.interval.ended_at },
      frequency_hz: o.frequency_hz,
      mode: o.mode,
      level_unit: o.level_unit,
      level: o.level,
      squelch_open: o.squelch_open,
      decoded_mode: o.decoded_mode,
    };
  } else if (event.event_type.startsWith('device.')) frame.invalidations = ['status'];
  else if (event.event_type.startsWith('session.')) frame.invalidations = ['sessions', 'status'];
  else if (event.event_type.startsWith('investigation.'))
    frame.invalidations = ['events', 'status'];
  return frame;
}

export class SequenceGate {
  constructor(public last: EventSequence = 0) {
    eventSequence(last, true);
  }
  accept(sequence: EventSequence): 'duplicate' | 'accept' | 'gap' {
    const exact = BigInt(eventSequence(sequence));
    if (exact <= BigInt(this.last)) return 'duplicate';
    if (exact !== BigInt(this.last) + 1n) return 'gap';
    this.last = sequence;
    return 'accept';
  }
  reset(last: EventSequence) {
    this.last = eventSequence(last, true);
  }
}

export class FrameBuffer {
  rows: DisplaySweep[] = [];
  private session = '';
  private stream = '';
  constructor(private capacity = 256) {
    if (!Number.isSafeInteger(capacity) || capacity < 1) throw Error('Invalid render capacity');
  }
  scope(session: string, stream: string) {
    if (this.session !== session || this.stream !== stream) this.rows = [];
    this.session = session;
    this.stream = stream;
  }
  clear() {
    this.rows = [];
  }
  append(sweep: DisplaySweep) {
    if (
      sweep.session_id !== this.session ||
      sweep.stream_id !== this.stream ||
      this.rows.some((s) => s.id === sweep.id)
    )
      return;
    this.rows = [...this.rows, sweep].slice(-this.capacity);
  }
}
