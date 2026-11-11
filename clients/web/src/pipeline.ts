import type { DisplaySweep, Observation, Sweep } from './types';

// These copies are exclusively for rendering. No measurement/threshold calculation is done here.
export function decimate(sweep: Sweep, limit = 2048): DisplaySweep {
  const { frequencies_hz: hz, levels } = sweep;
  if (
    !Array.isArray(hz) ||
    !Array.isArray(levels) ||
    !levels.length ||
    hz.length !== levels.length ||
    levels.length > 1_000_000 ||
    !Number.isInteger(limit) ||
    limit < 3
  )
    throw Error('Invalid sweep dimensions');
  let peak = 0;
  for (let i = 0; i < levels.length; i++) {
    if (
      !Number.isFinite(levels[i]) ||
      !Number.isSafeInteger(hz[i]) ||
      hz[i] < 0 ||
      (i > 0 && hz[i] < hz[i - 1])
    )
      throw Error('Invalid sweep values');
    if (levels[i] > levels[peak]) peak = i;
  }
  let indices: number[];
  if (hz.length <= limit) indices = hz.map((_, i) => i);
  else {
    const chosen = new Set([0, hz.length - 1, peak]);
    const buckets = limit - 3;
    for (let b = 0; b < buckets; b++) {
      const first = Math.floor((b * hz.length) / buckets),
        end = Math.floor(((b + 1) * hz.length) / buckets);
      let best = first;
      for (let i = first + 1; i < end; i++) if (levels[i] > levels[best]) best = i;
      chosen.add(best);
    }
    indices = [...chosen].sort((a, b) => a - b);
  }
  return {
    ...sweep,
    frequencies_hz: indices.map((i) => hz[i]),
    levels: indices.map((i) => levels[i]),
    source_indices: indices,
    original_point_count: sweep.original_point_count ?? levels.length,
    display_only: true,
    peak: { index: peak, frequency_hz: hz[peak], level: levels[peak] },
  };
}

export interface Frame {
  sequence: number;
  event_type: string;
  session_id?: string;
  sweep?: DisplaySweep;
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
export function parseFrame(raw: string): Frame {
  if (raw.length > 8_388_608) throw Error('Event exceeds client limit');
  const envelope = JSON.parse(raw);
  if (envelope.contract !== 'openradioinspector-platform' || envelope.contract_version !== 1)
    throw Error('Unsupported event contract');
  const event = envelope.data;
  if (
    !Number.isSafeInteger(event?.sequence) ||
    event.sequence < 1 ||
    typeof event.event_type !== 'string'
  )
    throw Error('Invalid event sequence');
  const frame: Frame = {
    sequence: event.sequence,
    event_type: event.event_type,
    session_id: event.session_id,
    invalidations: [],
  };
  if (event.event_type === 'sweep.received') frame.sweep = decimate(event.payload.sweep);
  else if (event.event_type === 'observation.received') {
    const o = event.payload.observation;
    if (
      !o?.id ||
      !o.interval?.started_at ||
      !Number.isSafeInteger(o.frequency_hz) ||
      !o.level_unit ||
      (o.level != null && !Number.isFinite(o.level))
    )
      throw Error('Invalid receiver observation');
    frame.observation = o;
  } else if (event.event_type.startsWith('device.')) frame.invalidations = ['status'];
  else if (event.event_type.startsWith('session.')) frame.invalidations = ['sessions', 'status'];
  else if (event.event_type.startsWith('investigation.'))
    frame.invalidations = ['events', 'status'];
  return frame;
}

export class SequenceGate {
  constructor(public last = 0) {}
  accept(sequence: number): 'duplicate' | 'accept' | 'gap' {
    if (sequence <= this.last) return 'duplicate';
    if (sequence !== this.last + 1) return 'gap';
    this.last = sequence;
    return 'accept';
  }
  reset(last: number) {
    this.last = last;
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
