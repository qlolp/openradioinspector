import type { BandCoverage, DisplaySweep, Project, WorkerStatus } from './types';

export function scanKey(sweep: Pick<DisplaySweep, 'scan_context'>): string | undefined {
  const context = sweep.scan_context;
  return context ? JSON.stringify([context.plan_id, context.band_id]) : undefined;
}
export function scanChoices(
  rows: readonly DisplaySweep[],
  project?: Project,
): { key: string; label: string }[] {
  if (project?.scan_plan)
    return project.scan_plan.config.bands.slice(0, 16).map((band) => ({
      key: JSON.stringify([project.scan_plan!.id, band.id]),
      label: band.name,
    }));
  const options = new Map<string, string>();
  for (const row of rows) {
    const key = scanKey(row);
    if (key && (options.size < 16 || options.has(key))) options.set(key, row.scan_context!.band_id);
  }
  return [...options].map(([key, label]) => ({ key, label }));
}

export function gridPointCount(start: string, stop: string): number | undefined {
  if (!/^\d+$/.test(start) || !/^\d+$/.test(stop)) return;
  const low = Number(start),
    high = Number(stop),
    span = high - low;
  if (low < 100000 || high > 1300000000 || span <= 0 || span % 10000 !== 0) return;
  return span / 10000 + 1;
}

export function coverageBands(worker?: WorkerStatus): BandCoverage[] {
  const coverage = worker?.current?.scan_coverage as { bands?: BandCoverage[] } | undefined;
  return coverage?.bands ?? [];
}
