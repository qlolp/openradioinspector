import type { EventSequence } from './sequence';
export interface Interval {
  started_at: string;
  ended_at: string;
}
export interface Sweep {
  id: string;
  session_id: string;
  stream_id: string;
  interval: Interval;
  frequencies_hz: (number | bigint)[];
  levels: number[];
  level_unit: string;
  settings: Record<string, unknown>;
  display_only?: boolean;
  original_point_count?: number;
}
export type DisplayNumbers = number[] | Float64Array<ArrayBuffer>;
export interface DisplaySweep extends Omit<Sweep, 'settings' | 'frequencies_hz' | 'levels'> {
  scan_context?: { plan_id: string; band_id: string };
  frequencies_hz: DisplayNumbers;
  levels: DisplayNumbers;
  source_indices: number[] | Uint32Array<ArrayBuffer>;
  peak: { index: number; frequency_hz: number; level: number };
  display_only: true;
}
export interface Observation {
  id: string;
  session_id: string;
  stream_id: string;
  interval: Interval;
  frequency_hz: number | bigint;
  mode: string;
  level_unit: string;
  level?: number | null;
  squelch_open?: boolean | null;
  decoded_mode?: string | null;
}
export interface Stream {
  id: string;
  device: string;
  role: string;
  port: string;
  level_unit: string;
  settings: Record<string, unknown>;
}
export interface Session {
  id: string;
  name: string;
  state: string;
  streams: Stream[];
}
export interface Project {
  scan_plan?: import('../../../contracts/generated/typescript/runtime').ScanPlanRevision;
  band_baselines?: Record<string, string>;
  id: string;
  name: string;
  site: string;
  state: string;
  active_session_id: string;
  analyzer_stream_id: string;
  receiver_stream_id: string;
  baseline_id: string | null;
  coverage?: string;
  last_error?: string;
  preparation?: {
    antenna_notes: string;
    receive_path_confirmed: boolean;
    location_confirmed: boolean;
  };
  audio?: { name: string } | null;
  profile?: { analyzer: Stream; receiver: Stream };
}
export interface WorkerStatus {
  stream_id: string;
  device: string;
  state: string;
  alive: boolean;
  adapter: string;
  last_error?: string;
  measurements: number;
  last_measurement_at?: string;
  current: Record<string, unknown>;
}
export interface GuardStatus {
  project: Project;
  active: boolean;
  receiver_busy: boolean;
  error?: string;
  audio_configured: boolean;
  manual: { role: string; expired: boolean; returning: boolean; expires_seconds: number }[];
  workers: WorkerStatus[];
  hardware_validation: string;
}
export interface Preflight {
  ready: boolean;
  checks: { name: string; ok: boolean; detail: string }[];
  baseline_ready: boolean;
  baseline_collection: {
    bands?: {
      band_id: string;
      name: string;
      sweeps: number;
      elapsed_seconds: number;
      minimum_sweeps: number;
      minimum_seconds: number;
      ready: boolean;
      baseline_id: string | null;
    }[];
    sweeps: number;
    elapsed_seconds: number;
    minimum_sweeps: number;
    minimum_seconds: number;
    ready: boolean;
  };
  hardware_validation: string;
}
export interface MonitoringEvent {
  id: string;
  project_id: string;
  kind: string;
  state: string;
  first_seen: string;
  last_seen: string;
  center_frequency_hz: number;
  start_frequency_hz: number;
  end_frequency_hz: number;
  peak_level: number;
  source_count: number;
  disposition: string;
  operator_comment?: string;
  analysis?: {
    state: string;
    error?: string;
    mode?: string;
    decoded_mode?: string;
    level?: number;
    audio?: { id: string };
  };
  source_refs?: { sweep_id: string; session_id: string; stream_id: string }[];
}
export interface Allowed {
  id: string;
  name: string;
  start_hz: number;
  end_hz: number;
  watch_changes: boolean;
}
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}
export interface SearchStatus {
  active: boolean;
  session_id?: string;
  stream_id?: string;
  coverage: string;
  maximum_level?: number;
  marks: { id: string; label: string; frequency_hz: number; level: number }[];
  series: { sweep_id: string; peak_level: number; frequency_hz: number; started_at: string }[];
  error?: string;
}
export interface Comparison {
  compatible: boolean;
  reason?: string;
  items: {
    frequency_hz: number;
    current_dbm: number;
    baseline_p50_dbm: number;
    baseline_p95_dbm: number;
    delta_db: number;
  }[];
  baseline_id: string;
  sweep_id: string;
}
export interface Health {
  status: string;
  last_event_id: EventSequence;
  sessions: number;
  running_sessions: number;
}
export interface Capabilities {
  features: Record<string, unknown>;
  session_storage?: string;
  persistent_sessions?: boolean;
}
