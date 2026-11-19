import type { DisplaySweep } from './types';
import type { FrequencyRange } from './spectrum-view';

export const PANORAMA_POINTS = 4096;
const MAX_WINDOWS = Math.ceil(129991 / 290);
const GRID_STEP = 10000;

export interface PanoramaWindow {
  index: number;
  id: string;
  startedAt: string;
  endedAt: string;
  low: number;
  high: number;
  pointCount: number;
  // Each entry is the measured maximum and the original index of its source.
  levels: Float64Array;
  peakIndices: Uint16Array;
}
export interface BandPanorama {
  count: number;
  lastIndex: number;
  windows: ReadonlyMap<number, PanoramaWindow>;
}
export interface PlanPanorama {
  sessionId: string;
  streamId: string;
  id: string;
  revision: number;
  bands: ReadonlyMap<string, BandPanorama>;
}

/** Keep only the latest complete physical window in each slot. No raw DTO is retained. */
export function updatePanorama(
  previous: PlanPanorama | undefined,
  sweep: DisplaySweep,
): PlanPanorama | undefined {
  const context = sweep.scan_context;
  if (
    !context ||
    context.window_index == null ||
    context.window_count == null ||
    context.revision == null ||
    context.step_hz !== GRID_STEP ||
    sweep.level_unit !== 'dBm'
  )
    return previous;
  const index = context.window_index,
    count = context.window_count,
    n = sweep.levels.length,
    low = sweep.frequencies_hz[0];
  if (
    !Number.isSafeInteger(count) ||
    count < 1 ||
    count > MAX_WINDOWS ||
    !Number.isSafeInteger(index) ||
    index < 0 ||
    index >= count ||
    n < 2 ||
    n > 290 ||
    sweep.original_point_count !== n ||
    sweep.frequencies_hz.length !== n ||
    !Number.isSafeInteger(low) ||
    low < 100000 ||
    low + (n - 1) * GRID_STEP > 1300000000
  )
    return previous;

  const sameStream =
    previous?.sessionId === sweep.session_id && previous?.streamId === sweep.stream_id;
  if (sameStream && previous && previous.revision > context.revision) return previous;
  const samePlan =
    sameStream && previous?.id === context.plan_id && previous.revision === context.revision;
  const bands = samePlan ? previous.bands : undefined;
  const band = bands?.get(context.band_id);
  if ((!band && (bands?.size ?? 0) >= 16) || (band && band.count !== count)) return previous;
  const last = band?.windows.get(index);
  // Decoded UTC strings are normalized to six fractional digits by the transport.
  if (last && (last.id === sweep.id || last.endedAt > sweep.interval.ended_at)) return previous;

  const bins = Math.min(n, 64, Math.floor(PANORAMA_POINTS / count));
  const levels = new Float64Array(bins).fill(-Infinity),
    peakIndices = new Uint16Array(bins);
  for (let i = 0; i < n; i++) {
    if (sweep.frequencies_hz[i] !== low + i * GRID_STEP || !Number.isFinite(sweep.levels[i]))
      return previous;
    const bin = Math.floor((i * bins) / n);
    if (sweep.levels[i] > levels[bin]) {
      levels[bin] = sweep.levels[i];
      peakIndices[bin] = i;
    }
  }
  const windows = new Map(band?.windows);
  windows.set(index, {
    index,
    id: sweep.id,
    startedAt: sweep.interval.started_at,
    endedAt: sweep.interval.ended_at,
    low,
    high: low + (n - 1) * GRID_STEP,
    pointCount: n,
    levels,
    peakIndices,
  });
  return {
    sessionId: sweep.session_id,
    streamId: sweep.stream_id,
    id: context.plan_id,
    revision: context.revision,
    bands: new Map(bands).set(context.band_id, { count, lastIndex: index, windows }),
  };
}

/** Project measured bucket maxima onto the viewport. No bucket crosses a physical window. */
export function panoramaColumns(
  band: BandPanorama | undefined,
  range: FrequencyRange,
  width: number,
): Float64Array {
  const maxima = new Float64Array(Math.min(PANORAMA_POINTS, Math.max(1, width))).fill(-Infinity);
  if (!band || range.high <= range.low) return maxima;
  const column = (hz: number) =>
    Math.max(
      0,
      Math.min(
        maxima.length - 1,
        Math.floor(((hz - range.low) * maxima.length) / (range.high - range.low)),
      ),
    );
  for (const window of band.windows.values()) {
    for (let bin = 0; bin < window.levels.length; bin++) {
      const first =
        window.low + Math.ceil((bin * window.pointCount) / window.levels.length) * GRID_STEP;
      const last =
        window.low +
        (Math.ceil(((bin + 1) * window.pointCount) / window.levels.length) - 1) * GRID_STEP;
      if (last < range.low || first > range.high) continue;
      for (let x = column(first), end = column(last); x <= end; x++)
        maxima[x] = Math.max(maxima[x], window.levels[bin]);
    }
  }
  return maxima;
}
