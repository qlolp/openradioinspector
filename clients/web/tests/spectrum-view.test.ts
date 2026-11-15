import { expect, it } from 'vitest';
import {
  clampRange,
  panRange,
  peakPosition,
  sourceRange,
  traceVertices,
  waterfallPixels,
  zoomRange,
} from '../src/spectrum-view';
import { decimate } from '../src/pipeline';

it('anchors local zoom, clamps panning and keeps a one-Hz minimum without changing sources', () => {
  const full = { low: 100, high: 200 };
  const zoomed = zoomRange(full, full, 0.5, 0.25);
  expect(zoomed).toEqual({ low: 112.5, high: 162.5 });
  expect(panRange(zoomed, full, -1000)).toEqual({ low: 100, high: 150 });
  expect(panRange(zoomed, full, 1000)).toEqual({ low: 150, high: 200 });
  expect(zoomRange(zoomed, full, 1e-20).high - zoomRange(zoomed, full, 1e-20).low).toBe(1);
  expect(clampRange({ low: NaN, high: Infinity }, full)).toEqual(full);
});

it('projects one-Hz detail at high integer Hz before converting to float32', () => {
  const low = 9_007_199_254_740_989;
  const frequencies = [low, low + 1, low + 2],
    levels = [-100, -60, -80];
  const view = { low: low + 1, high: low + 2 };
  const vertices = traceVertices(frequencies, levels, view, { low: -130, high: 10 });
  expect(Array.from(vertices).filter((_, i) => i % 2 === 0)).toEqual([-1, 1]);
  expect(frequencies).toEqual([low, low + 1, low + 2]);
});

it('clips a segment at frequency boundaries and preserves the first peak and raw bits', () => {
  const raw = {
    id: 'source',
    session_id: 's',
    stream_id: 'a',
    interval: { started_at: '2026-11-11T00:00:00Z', ended_at: '2026-11-11T00:00:01Z' },
    frequencies_hz: [100, 110, 120, 130],
    levels: [-0, -80, -10, -10],
    level_unit: 'dBm',
    settings: {},
  };
  const source = decimate(raw);
  const before = structuredClone(raw);
  const view = { low: 105, high: 115 };
  const vertices = traceVertices(source.frequencies_hz, source.levels, view, {
    low: -130,
    high: 10,
  });
  expect(Array.from(vertices).filter((_, i) => i % 2 === 0)).toEqual([-1, 0, 1]);
  expect(peakPosition(source, view, { low: -130, high: 10 })).toBeUndefined();
  expect(source.peak).toEqual({ index: 0, frequency_hz: 100, level: -0 });
  expect(raw).toEqual(before);
  expect(Object.is(raw.levels[0], -0)).toBe(true);
});

it('bounds display vertices while retaining first maximum ties and finite extreme projections', () => {
  const frequencies = Array.from({ length: 10000 }, (_, i) => i),
    levels = frequencies.map(() => -100);
  levels[37] = -5;
  levels[75] = -5;
  const vertices = traceVertices(
    frequencies,
    levels,
    { low: 0, high: 9999 },
    { low: -130, high: 10 },
    32,
  );
  expect(vertices.length).toBeLessThanOrEqual(64);
  expect(Array.from(vertices).filter((_, i) => i % 2 === 0)).toContain(
    Math.fround((37 / 9999) * 2 - 1),
  );
  const extremes = [-Number.MAX_VALUE, Number.MAX_VALUE];
  expect(
    Array.from(
      traceVertices([0, 10], extremes, { low: 4, high: 6 }, { low: -130, high: 10 }),
    ).every(Number.isFinite),
  ).toBe(true);
  expect(extremes).toEqual([-Number.MAX_VALUE, Number.MAX_VALUE]);
  expect(sourceRange({ frequencies_hz: [0] })).toEqual({ low: 0, high: 1 });
});

it('retains a narrow source peak when reducing a waterfall row and does not change raw samples', () => {
  const sweep = decimate({
    id: 'narrow-peak',
    session_id: 's',
    stream_id: 'a',
    interval: { started_at: '2026-11-11T00:00:00Z', ended_at: '2026-11-11T00:00:01Z' },
    frequencies_hz: [100, 101, 102, 103],
    levels: [-100, -20, -100, -100],
    level_unit: 'dBm',
    settings: {},
  });
  const before = structuredClone(sweep);
  expect(Array.from(waterfallPixels(sweep, 2)).slice(0, 4)).toEqual([255, 255, 50, 255]);
  expect(sweep).toEqual(before);
  expect(sweep.peak.index).toBe(1);
});

it('retains a visible first peak even when two clipped boundary vertices consume the minimum budget', () => {
  const points = traceVertices(
    [0, 2, 4, 6, 8, 10],
    [-100, -100, -10, -10, -100, -100],
    { low: 1, high: 9 },
    { low: -130, high: 10 },
    4,
  );
  expect(points.length).toBeLessThanOrEqual(8);
  expect(Array.from(points).filter((_, i) => i % 2 === 0)).toContain(-0.25);
});

it('keeps immutable scan provenance when preparing a display frame', () => {
  const raw = {
    id: 'a',
    session_id: 's',
    stream_id: 'r',
    interval: { started_at: '2026-11-14T00:00:00Z', ended_at: '2026-11-14T00:00:01Z' },
    frequencies_hz: [100000000, 100010000],
    levels: [-90, -40],
    level_unit: 'dBm',
    settings: { scan_plan_id: 'plan-1', scan_plan_revision: 1, scan_band_id: 'fm' },
  };
  expect(decimate(raw)).toMatchObject({ scan_context: { plan_id: 'plan-1', band_id: 'fm' } });
  expect(raw.settings).toEqual({
    scan_plan_id: 'plan-1',
    scan_plan_revision: 1,
    scan_band_id: 'fm',
  });
});
