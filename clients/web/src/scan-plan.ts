import type { DisplaySweep, Project } from './types';

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
