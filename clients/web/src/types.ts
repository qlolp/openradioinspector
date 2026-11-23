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
  rf_explorer?: import('./rf-explorer').RfExplorerReadback;
  scan_context?: {
    plan_id: string;
    band_id: string;
    revision?: number;
    step_hz?: number;
    window_index?: number;
    window_count?: number;
  };
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
  metadata?: Record<string, unknown>;
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
  metadata?: Record<string, unknown>;
}
export interface Project {
  scan_plan?: import('../../../contracts/generated/typescript/runtime').ScanPlanRevision;
  band_baselines?: Record<string, string>;
  id: string;
  name: string;
  site: string;
  state: string;
  active_session_id: string;
  primary_source_kind?: import('./api').DutySource;
  primary_stream_id?: string;
  duty?: { enabled: boolean; status: string; last_error?: string | null };
  secondary_stream_id?: string | null;
  analyzer_stream_id?: string | null;
  receiver_stream_id?: string | null;
  baseline_id: string | null;
  coverage?: string;
  last_error?: string;
  preparation?: {
    antenna_notes: string;
    receive_path_confirmed: boolean;
    location_confirmed: boolean;
  };
  audio?: { name: string } | null;
  profile?: { analyzer?: Stream | null; receiver?: Stream | null };
}
export interface WorkerStatus {
  stream_id: string;
  device: string;
  state: string;
  alive: boolean;
  adapter: string;
  last_error?: string;
  measurements: number;
  started_at?: string;
  last_measurement_at?: string;
  current: Record<string, unknown>;
}
export type MeasurementQualification =
  import('../../../contracts/generated/typescript/runtime').MeasurementQualification;
export interface BandCoverage {
  measurement_qualification?: MeasurementQualification;
  id: string;
  name: string;
  state: string;
  deadline_missed: boolean;
  age_seconds: number | null;
  last_revisit_seconds: number | null;
  step_hz?: number;
  observed_windows?: number;
  window_count?: number;
  completed_passes?: number;
  last_window?: {
    measurement_qualification?: MeasurementQualification;
    index: number;
    start_hz: number;
    stop_hz: number;
    points: number;
    measurement_id: string;
    started_at: string;
    ended_at: string;
  } | null;
}
export interface StorageReport {
  capacity?: { database_bytes?: number; state?: string };
  writer_queue?: {
    queued_operations: number;
    writer_capacity: number;
    retained_bytes: number;
    writer_byte_limit: number;
  };
  ready?: boolean;
  write_probe?: boolean | null;
  available_bytes?: number | null;
  filesystem_free_bytes?: number | null;
  minimum_free_bytes?: number;
  estimated_retention_seconds?: number | null;
  required_retention_seconds?: number;
  mean_stored_sweep_bytes?: number | null;
  sampled_observations?: number;
  projected_bytes_per_day?: number | null;
  wal_bytes?: number | null;
  backup_reserve_bytes?: number | null;
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
  preparation_ready?: boolean;
  storage?: StorageReport | null;
  checks: { name: string; ok: boolean; detail: string }[];
  baseline_ready: boolean;
  baseline_collection: {
    bands?: {
      band_id: string;
      measurement_qualification?: MeasurementQualification;
      window_count?: number;
      eligible_windows?: number;
      excluded_windows?: number;
      ready_windows?: number;
      confirmed_windows?: number;
      name: string;
      sweeps: number;
      elapsed_seconds: number;
      minimum_sweeps: number;
      minimum_seconds: number;
      ready: boolean;
      baseline_id: string | null;
    }[];
    points?: number;
    ready_points?: number;
    windows?: number;
    ready_windows?: number;
    passes?: number;
    minimum_passes?: number;
    sweeps?: number;
    elapsed_seconds: number;
    minimum_sweeps?: number;
    minimum_seconds: number;
    ready: boolean;
  };
  hardware_validation: string;
}
export interface MonitoringEvent {
  id: string;
  project_id: string;
  run_session_id?: string;
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
    receiver_status?: Observation[];
    observation_ids?: string[];
    audio?: { id: string };
  };
  level_unit?: string;
  peak_source?: {
    sweep_id?: string;
    observation_id?: string;
    session_id: string;
    stream_id: string;
    frequency_hz?: number;
  };
  source_refs?: {
    sweep_id?: string;
    observation_id?: string;
    session_id: string;
    stream_id: string;
  }[];
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
