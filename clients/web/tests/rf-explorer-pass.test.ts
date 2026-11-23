import { expect, it } from 'vitest';
import { rfExplorerSession } from '../src/rf-explorer';
import { responseValue } from '../src/wire';

const settings = (center: string, span: string) =>
  rfExplorerSession('Survey', 'COM12', { survey: true, center, span }).streams[0].settings;

it('converts center and span to the exact offset 10 kHz grid used by the core', () => {
  expect(settings('101116000', '2230000')).toMatchObject({
    start_hz: 100001000,
    stop_hz: 102231000,
    survey_step_hz: 10000,
  });
  for (const [center, span] of [
    ['101116000', '2230001'],
    ['101116000.01', '2230000'],
    ['101116000', '2231000'],
    ['100000001', '2230000'],
    ['1e8', '2000000'],
    ['3000000000', '2000000'],
  ])
    expect(() => settings(center, span)).toThrow();
});

const path = '/api/v1/sessions/session/streams/stream/rf-explorer/passes/';
const interval = { started_at: '2026-11-20T00:00:00Z', ended_at: '2026-11-20T00:00:01Z' };
const summary = {
  pass_id: 'partial',
  pass: 1,
  start_hz: 80000000,
  stop_hz: 82230000,
  step_hz: 10000,
  tiles: 2,
  received_tiles: 1,
  unique_points: 112,
  expected_points: 224,
  ...interval,
  complete: false,
  duration_s: null,
  revisit_s: 1.25,
  active_board: 'expansion',
  active_input: 'right',
  rbw_hz: 15000,
  detector: 'max_hold',
  offset_db: 0,
  simulated: true,
};
const point = {
  frequency_hz: 80000000,
  level_dbm: -43.5,
  sweep_id: 'sweep',
  point_index: 0,
  ...interval,
  tile: 1,
  last_measured_at: interval.ended_at,
};
const envelope = (data: unknown) =>
  JSON.stringify({
    contract: 'openradioinspector-platform',
    contract_version: 1,
    data,
  });
const detail = {
  summary,
  columns: [point, null],
  coverage: [{ start_hz: 80000000, stop_hz: 81110000 }],
  hold_columns: null,
  hold_coverage: null,
};

it('decodes measured float64 levels and nullable timing without filling missing columns', () => {
  expect(responseValue(path + 'partial?columns=2', envelope(detail))).toMatchObject({
    data: {
      columns: [{ level_dbm: -43.5, point_index: 0 }, null],
      summary: { duration_s: null, revisit_s: 1.25 },
      hold_columns: null,
    },
  });
});

it('rejects a detail for a different pass and out-of-range original point indexes', () => {
  expect(() => responseValue(path + 'another', envelope(detail))).toThrow();
  const bad = { ...detail, columns: [{ ...point, point_index: 112 }] };
  expect(() => responseValue(path + 'partial', envelope(bad))).toThrow();
});

it('decodes a session original with normalized source UTC and exact source levels', () => {
  const data = {
    id: 'sweep',
    session_id: 'session',
    stream_id: 'stream',
    interval,
    frequencies_hz: [80000000],
    levels: [-43.5],
    level_unit: 'dBm',
    settings: {},
  };
  expect(responseValue('/api/v1/sessions/session/sweeps/sweep', envelope(data))).toMatchObject({
    data: { levels: [-43.5], interval: { ended_at: '2026-11-20T00:00:01.000000Z' } },
  });
});

it('keeps sub-column holes and overlapping window coverage distinct from bucket maxima', async () => {
  const { missingRanges, pointAt } = await import('../src/RfPassPlot');
  const low = 80000000;
  const measured = [
    { start_hz: low, stop_hz: 81110000 },
    { start_hz: 82240000, stop_hz: 83350000 },
  ];
  const far = { ...point, frequency_hz: 82240000, tile: 3, sweep_id: 'far' };
  expect(pointAt(81500000, [point, far], measured)).toBeNull();
  expect(pointAt(82250000, [point, far], measured)).toBe(far);
  expect(missingRanges({ start_hz: low, stop_hz: 84470000 }, measured, 10000)).toEqual([
    { start_hz: 81120000, stop_hz: 82230000 },
    { start_hz: 83360000, stop_hz: 84470000 },
  ]);
});

it('decodes stable, typed and bounded cursor pages for pass selection and playback', () => {
  expect(
    responseValue(
      path.slice(0, -1) + '?limit=1',
      envelope({ items: [summary], next_before: 'old/+' }),
    ),
  ).toMatchObject({
    data: {
      items: [{ pass_id: 'partial', started_at: '2026-11-20T00:00:00.000000Z' }],
      next_before: 'old/+',
    },
  });
  const windows = { items: [{ tile: 2, sweep_id: 'b', ...interval }], next_after_tile: 2 };
  expect(
    responseValue(path + 'partial/windows?after_tile=1&limit=1', envelope(windows)),
  ).toMatchObject({
    data: { items: [{ tile: 2, sweep_id: 'b' }], next_after_tile: 2 },
  });
  expect(() =>
    responseValue(path + 'partial/windows', envelope({ ...windows, next_after_tile: 1 })),
  ).toThrow();
});

it('only identifies an input from explicit device readback and keeps simulation provenance', async () => {
  const { rfExplorerReadback } = await import('../src/rf-explorer');
  const cfg = {
    adapter: 'rf-explorer-3g-serial',
    active_board: 'main',
    active_input: 'left',
    board_min_hz: 240000000,
    board_max_hz: 960000000,
    step_hz: 10000,
    simulated: true,
  };
  for (const flags of [{}, { settings_readback: true }, { settings_source: 'device_readback' }]) {
    const result = rfExplorerReadback({ ...cfg, ...flags });
    expect(result?.active_board).toBeUndefined();
    expect(result?.board_min_hz).toBeUndefined();
  }
  expect(
    rfExplorerReadback({ ...cfg, settings_readback: true, settings_source: 'device_readback' }),
  ).toMatchObject({
    active_board: 'main',
    active_input: 'left',
    board_min_hz: 240000000,
    simulated: true,
    confirmed: true,
  });
});
