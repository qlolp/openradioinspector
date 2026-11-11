import type { DisplaySweep, DisplayNumbers } from './types';

export interface FrequencyRange {
  low: number;
  high: number;
}
export interface LevelRange {
  low: number;
  high: number;
}
export function sourceRange(sweep?: Pick<DisplaySweep, 'frequencies_hz'>): FrequencyRange {
  const low = sweep?.frequencies_hz[0] ?? 88_000_000;
  const high = sweep?.frequencies_hz.at(-1) ?? 108_000_000;
  return high > low ? { low, high } : { low: Math.max(0, low - 1), high: low + 1 };
}
export function clampRange(requested: FrequencyRange, full: FrequencyRange): FrequencyRange {
  const available = full.high - full.low;
  if (
    !Number.isFinite(requested.low) ||
    !Number.isFinite(requested.high) ||
    requested.high <= requested.low
  )
    return { ...full };
  const width = Math.min(
    available,
    Math.max(Math.min(1, available), requested.high - requested.low),
  );
  const low = Math.max(full.low, Math.min(full.high - width, requested.low));
  return { low, high: low + width };
}
export function zoomRange(
  view: FrequencyRange,
  full: FrequencyRange,
  factor: number,
  anchor = 0.5,
): FrequencyRange {
  if (!Number.isFinite(factor) || factor <= 0) return { ...full };
  const at = Math.min(1, Math.max(0, anchor));
  const span = view.high - view.low;
  const width = Math.min(
    full.high - full.low,
    Math.max(Math.min(1, full.high - full.low), span * factor),
  );
  const low = view.low + span * at - width * at;
  return clampRange({ low, high: low + width }, full);
}
export function panRange(
  view: FrequencyRange,
  full: FrequencyRange,
  delta: number,
): FrequencyRange {
  return Number.isFinite(delta)
    ? clampRange({ low: view.low + delta, high: view.high + delta }, full)
    : { ...view };
}
export function sourceLevels(sweep?: DisplaySweep): LevelRange {
  if (!sweep || sweep.level_unit === 'dBm') return { low: -130, high: 10 };
  let low = Infinity,
    high = -Infinity;
  for (const level of sweep.levels) {
    low = Math.min(low, level);
    high = Math.max(high, level);
  }
  const padding = Math.max(1, (high / 2 - low / 2) / 10);
  return {
    low: Math.max(-Number.MAX_VALUE, low - padding),
    high: Math.min(Number.MAX_VALUE, high + padding),
  };
}
function levelPosition(level: number, bounds: LevelRange): number {
  const span = bounds.high - bounds.low;
  const relative = Number.isFinite(span)
    ? (level - bounds.low) / span
    : (level / 2 - bounds.low / 2) / (bounds.high / 2 - bounds.low / 2);
  // GPU clipping is display only. Bound extreme off-axis coordinates before f32
  // conversion; original f64 arrays and the source peak remain untouched.
  return Math.max(-1e6, Math.min(1e6, relative * 2 - 1));
}
function lowerBound(values: DisplayNumbers, target: number, after = false) {
  let first = 0,
    last = values.length;
  while (first < last) {
    const mid = (first + last) >>> 1;
    if (values[mid] < target || (after && values[mid] === target)) first = mid + 1;
    else last = mid;
  }
  return first;
}
/** CPU projection uses local f64 Hz offsets; absolute Hz never enter float32. */
export function traceVertices(
  frequencies: DisplayNumbers,
  levels: DisplayNumbers,
  view: FrequencyRange,
  bounds: LevelRange,
  limit = 4096,
): Float32Array {
  if (!frequencies.length || frequencies.length !== levels.length || limit < 4)
    return new Float32Array();
  const first = lowerBound(frequencies, view.low),
    end = lowerBound(frequencies, view.high, true);
  const output: number[] = [];
  const boundary = (hz: number, index: number) => {
    const a = index - 1,
      b = index;
    const fraction = (hz - frequencies[a]) / (frequencies[b] - frequencies[a]);
    const ya = levelPosition(levels[a], bounds),
      yb = levelPosition(levels[b], bounds);
    return ya + (yb - ya) * fraction;
  };
  const left = first > 0 && first < frequencies.length && frequencies[first] > view.low;
  const right = end > 0 && end < frequencies.length && frequencies[end - 1] < view.high;
  if (left) output.push(-1, boundary(view.low, first));
  let indices: number[] = [];
  const budget = limit - Number(left) - Number(right);
  if (end - first <= budget) {
    for (let i = first; i < end; i++) indices.push(i);
  } else if (budget === 2) {
    let peak = first;
    for (let i = first + 1; i < end; i++) if (levels[i] > levels[peak]) peak = i;
    indices = peak === first ? [first, end - 1] : [first, peak];
  } else {
    // Display-only bucket maxima retain first ties and the global visible peak.
    const selected = new Set([first, end - 1]);
    for (let bucket = 0; bucket < budget - 2; bucket++) {
      const start = first + Math.floor((bucket * (end - first)) / (budget - 2));
      const stop = first + Math.floor(((bucket + 1) * (end - first)) / (budget - 2));
      let peak = start;
      for (let i = start + 1; i < stop; i++) if (levels[i] > levels[peak]) peak = i;
      selected.add(peak);
    }
    indices = [...selected].sort((a, b) => a - b);
  }
  for (const i of indices)
    output.push(
      ((frequencies[i] - view.low) / (view.high - view.low)) * 2 - 1,
      levelPosition(levels[i], bounds),
    );
  if (right) output.push(1, boundary(view.high, end));
  return new Float32Array(output);
}
export function peakPosition(
  sweep: DisplaySweep,
  view: FrequencyRange,
  bounds: LevelRange,
): [number, number] | undefined {
  const hz = sweep.peak.frequency_hz;
  if (hz < view.low || hz > view.high) return;
  return [
    ((hz - view.low) / (view.high - view.low)) * 2 - 1,
    Math.max(-1, Math.min(1, levelPosition(sweep.peak.level, bounds))),
  ];
}

/** One display texture row. Bucket maxima keep even a one-point source peak. */
export function waterfallPixels(sweep: DisplaySweep, width = 1024): Uint8Array<ArrayBuffer> {
  const output = new Uint8Array(width * 4),
    maxima = new Float64Array(width);
  maxima.fill(-Infinity);
  const low = sweep.frequencies_hz[0],
    high = sweep.frequencies_hz.at(-1)!;
  for (let i = 0; i < sweep.levels.length; i++) {
    const bucket = Math.min(
      width - 1,
      Math.floor(((sweep.frequencies_hz[i] - low) / (high - low || 1)) * width),
    );
    if (sweep.levels[i] > maxima[bucket]) maxima[bucket] = sweep.levels[i];
  }
  let point = 0;
  const bounds = sourceLevels(sweep);
  for (let x = 0; x < width; x++) {
    const hz = low + ((high - low) * x) / Math.max(1, width - 1);
    while (
      point + 1 < sweep.levels.length &&
      Math.abs(sweep.frequencies_hz[point + 1] - hz) < Math.abs(sweep.frequencies_hz[point] - hz)
    )
      point++;
    const value = maxima[x] === -Infinity ? sweep.levels[point] : maxima[x];
    const heat = Math.max(
      0,
      Math.min(
        1,
        sweep.level_unit === 'dBm' ? (value + 120) / 100 : (levelPosition(value, bounds) + 1) / 2,
      ),
    );
    output[x * 4] = Math.min(255, Math.max(0, heat - 0.42) * 440);
    output[x * 4 + 1] = Math.min(255, heat * 390);
    output[x * 4 + 2] = Math.max(35, 110 + heat * 140 - Math.max(0, heat - 0.6) * 500);
    output[x * 4 + 3] = 255;
  }
  return output;
}
