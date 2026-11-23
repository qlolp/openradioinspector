import { getLocale, t } from './i18n';
import type { GuardStatus, Preflight, StorageReport, WorkerStatus } from './types';

export function preparationReady(preflight?: Preflight): boolean {
  return preflight?.preparation_ready ?? preflight?.ready ?? false;
}

export function formatQuantity(value: number, digits = 1): string {
  return value.toLocaleString(getLocale(), { maximumFractionDigits: digits });
}

export function formatBytes(bytes?: number | null): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return t('Unavailable');
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const index = Math.min(units.length - 1, Math.floor(Math.log2(Math.max(1, bytes)) / 10));
  return formatQuantity(bytes / 1024 ** index, 2) + ' ' + t(units[index]);
}

export function measuredRate(worker?: WorkerStatus): number | undefined {
  if (
    !worker?.alive ||
    !worker.started_at ||
    !worker.last_measurement_at ||
    worker.measurements < 1
  )
    return;
  const elapsed = Date.parse(worker.last_measurement_at) - Date.parse(worker.started_at);
  if (elapsed > 0 && Number.isSafeInteger(worker.measurements))
    return (worker.measurements * 1000) / elapsed;
}

export function estimatedBaselineSeconds(
  progress: Preflight['baseline_collection'] | undefined,
  visitsPerPass: number | undefined,
  rate: number | undefined,
): number | undefined {
  if (!progress || !rate || !Number.isFinite(rate) || rate <= 0) return;
  if (progress.ready) return 0;
  let remainingVisits: number;
  let minimumSeconds = progress.minimum_seconds ?? 60,
    elapsed = progress.elapsed_seconds ?? 0;
  if (progress.bands?.length) {
    // A single configured band has one repeatable itinerary. Custom multiple-band priorities can differ.
    if (progress.bands.length !== 1) return;
    const band = progress.bands[0];
    if (!(band.eligible_windows ?? 1)) return;
    remainingVisits = (band.minimum_sweeps - band.sweeps) * (band.window_count ?? 1);
    elapsed = band.elapsed_seconds;
    minimumSeconds = band.minimum_seconds;
  } else if (progress.minimum_passes != null && progress.passes != null && visitsPerPass) {
    remainingVisits = (progress.minimum_passes - progress.passes) * visitsPerPass;
  } else if (progress.minimum_sweeps != null && progress.sweeps != null) {
    remainingVisits = progress.minimum_sweeps - progress.sweeps;
  } else return;
  return Math.ceil(Math.max(0, minimumSeconds - elapsed, remainingVisits / rate));
}

export interface RecoveryNotice {
  kind: 'archive' | 'analyzer' | 'receiver' | 'pause';
  title: string;
  detail: string;
  error?: string;
}

export function recoveryNotices(guard?: GuardStatus, storage?: StorageReport): RecoveryNotice[] {
  const result: RecoveryNotice[] = [];
  if (
    storage?.ready === false ||
    storage?.capacity?.state === 'full' ||
    storage?.write_probe === false
  )
    result.push({
      kind: 'archive',
      title: 'Archive needs attention',
      detail:
        'Check free space, the archive limit and write permission on the node. Run preflight again before starting collection.',
    });
  if (!guard) return result;
  for (const role of ['analyzer', 'receiver'] as const) {
    const worker = guard.workers.find(
      (entry) =>
        entry.stream_id ===
        (role === 'analyzer' ? guard.project.analyzer_stream_id : guard.project.receiver_stream_id),
    );
    const held = guard.manual.some((lease) => lease.role === role);
    if (
      worker &&
      !held &&
      (worker.state === 'error' || !!worker.last_error || (guard.active && !worker.alive))
    ) {
      const serial = worker.adapter?.endsWith('-serial');
      result.push({
        kind: role,
        title: role === 'analyzer' ? 'Analyzer needs attention' : 'Receiver needs attention',
        detail: serial
          ? 'Check the USB cable, assigned serial port and exclusive access on the node. Restore the connection and recheck status.'
          : 'Check the worker on the node and recheck status.',
        error: worker.last_error,
      });
    }
  }
  if (
    guard.project.coverage === 'narrow' ||
    guard.project.coverage?.startsWith('paused') ||
    guard.manual.some((lease) => lease.role === 'analyzer')
  )
    result.push({
      kind: 'pause',
      title: 'Broad coverage paused',
      detail:
        guard.project.primary_source_kind === 'ardv1'
          ? 'Return the receiver to the cyclic plan to restore broad coverage.'
          : guard.manual.some((lease) => lease.role === 'analyzer')
            ? 'Return the analyzer in Research to restore broad coverage.'
            : 'Check the active search and analyzer ownership in Research.',
    });
  return result;
}
