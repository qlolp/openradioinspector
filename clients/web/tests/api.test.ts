import { describe, expect, it } from 'vitest';
import { rfExplorerSession } from '../src/rf-explorer';
import { Api, ResourceScope, historyQuery, receiveSession } from '../src/api';

describe('v1 client boundary', () => {
  it('keeps IDs in one URL segment and passes original envelopes through the transport', async () => {
    const paths: string[] = [];
    const api = new Api({
      request: async (path) => {
        paths.push(path);
        return {
          contract: 'openradioinspector-platform',
          contract_version: 1,
          data: { id: 'a/b' },
        };
      },
    });
    expect(await api.project('a/b', 'history/sweep%2F1')).toEqual({ id: 'a/b' });
    expect(paths).toEqual(['/api/v1/monitoring-projects/a%2Fb/history/sweep%2F1']);
  });
  it('surfaces server error messages and rejects contract drift', async () => {
    const bad = new Api({
      request: async () => ({
        error: { code: 'monitoring_conflict', message: 'Manual owner required' },
      }),
    });
    await expect(bad.get('/health')).rejects.toThrow('Manual owner required');
    const drift = new Api({ request: async () => ({ contract_version: 2, data: {} }) });
    await expect(drift.get('/health')).rejects.toThrow(/contract/i);
  });
  it('cancels stale project reads and prevents an old response committing to a new selection', async () => {
    const scope = new ResourceScope();
    const old = scope.next();
    const fresh = scope.next();
    expect(old.signal.aborted).toBe(true);
    expect(scope.current(old.generation)).toBe(false);
    expect(scope.current(fresh.generation)).toBe(true);
    scope.dispose();
    expect(fresh.signal.aborted).toBe(true);
  });
  it('emits bounded history filters with explicit UTC and preserves cursor bytes', () => {
    expect(
      historyQuery(
        { start: '2026-11-11T01:02', min_hz: '88000000', max_hz: '108000000', event_id: 'e/2' },
        'a+=/',
      ),
    ).toBe(
      '?limit=32&start=2026-11-11T01%3A02%3A00.000Z&min_hz=88000000&max_hz=108000000&event_id=e%2F2&after=a%2B%3D%2F',
    );
    expect(() => historyQuery({ min_hz: '3.2' })).toThrow(/integer/i);
  });
  it('requires a physical RF Explorer serial port and a complete optional window', () => {
    for (const port of ['', 'sim://source', 'tcp://example.invalid', 'COM3\0']) {
      expect(() => rfExplorerSession('RF', port)).toThrow(/serial port/i);
    }
    for (const window of [
      { start: '100000000', stop: '' },
      { start: '', stop: '101110000' },
      { start: '101110000', stop: '100000000' },
      { start: '100000100', stop: '101110000' },
    ])
      expect(() => rfExplorerSession('RF', 'COM3', window)).toThrow(/whole kHz/i);
  });
  it('requests the full active module or an inclusive survey range with 10 kHz spacing', () => {
    const settings = (bounds?: { start: string; stop: string }) =>
      rfExplorerSession('RF', 'COM3', { survey: true, ...bounds }).streams[0].settings;
    expect(settings()).toEqual({
      adapter: 'rf-explorer-3g-serial',
      baud: 500000,
      survey_step_hz: 10000,
    });
    for (const [start, stop] of [
      ['100001000', '101111000'],
      ['15000000', '2700000000'],
    ]) {
      expect(settings({ start, stop })).toMatchObject({
        survey_step_hz: 10000,
        start_hz: Number(start),
        stop_hz: Number(stop),
      });
    }
  });
  it('rejects a survey with missing bounds, fewer than 112 bins, or frequencies outside the supported grid', () => {
    for (const [start, stop] of [
      ['', '101110000'],
      ['100000000', ''],
      ['100000000', '101100000'],
      ['100001000', '101120000'],
      ['100001001', '101111001'],
      ['14999000', '16109000'],
      ['2698891000', '2700001000'],
    ]) {
      expect(() => rfExplorerSession('RF', 'COM3', { survey: true, start, stop })).toThrow();
    }
  });
  it('creates only explicit simulated or conservative receive profiles without generator capabilities', () => {
    const session = receiveSession({
      name: 'Room',
      simulator: true,
      analyzerPort: '',
      receiverPort: '',
      startHz: 88000000,
      stopHz: 108000000,
      points: 201,
      rbwHz: 10000,
    });
    expect(session.streams[0].settings.adapter).toBe('tinysa-sim');
    expect(session.streams[0].settings).not.toHaveProperty('generator_output');
    expect(session.streams[0].settings).toHaveProperty('period_s', 1);
    expect(session.streams[1].level_unit).toBe('s_meter_count');
    expect(() =>
      receiveSession({
        name: 'Room',
        simulator: false,
        analyzerPort: '',
        receiverPort: '',
        startHz: 88000000,
        stopHz: 108000000,
        points: 201,
        rbwHz: 10000,
      }),
    ).toThrow(/port/i);
  });
});

const physical = {
  name: 'Receive duty',
  simulator: false,
  analyzerPort: 'COM3',
  receiverPort: 'COM4',
  startHz: 100_000_000,
  stopHz: 400_000_000,
  stepHz: 10_000,
  points: 201,
  rbwHz: 10_000,
};

it('uses only the selected source, and adds an independent event receiver only on request', () => {
  for (const primarySource of ['tinysa', 'rf_explorer', 'ardv1'] as const) {
    const session = receiveSession({ ...physical, primarySource });
    expect(session.streams).toHaveLength(1);
    expect(session.streams[0].settings.adapter).toBe(
      { tinysa: 'tinysa-serial', rf_explorer: 'rf-explorer-3g-serial', ardv1: 'ardv1-serial' }[
        primarySource
      ],
    );
    if (primarySource !== 'ardv1') {
      const checked = receiveSession({ ...physical, primarySource, checkEvents: true });
      expect(checked.streams.map((s) => s.port)).toEqual(['COM3', 'COM4']);
      expect(checked.streams[1].settings).not.toHaveProperty('fast_scan');
      expect(() =>
        receiveSession({ ...physical, primarySource, checkEvents: true, receiverPort: 'COM3' }),
      ).toThrow(/port/i);
    }
  }
  expect(
    receiveSession({ ...physical, primarySource: 'rf_explorer' }).streams[0].settings,
  ).toMatchObject({
    start_hz: 100_000_000,
    stop_hz: 400_000_000,
    survey_step_hz: 10_000,
    baud: 500000,
  });
});

it('requests a cyclic FM receiver plan without changing the final off-grid frequency', () => {
  const source = receiveSession({
    ...physical,
    primarySource: 'ardv1',
    startHz: 100_000,
    stopHz: 1_299_999_990,
  }).streams[0];
  expect(source).toMatchObject({
    role: 'receiver',
    port: 'COM4',
    level_unit: 's_meter_count',
    settings: {
      adapter: 'ardv1-serial',
      receiver_profile: 'ardv1-monitor-v1',
      frequency_hz: 100_000,
      mode: 'FM',
      period_s: 0,
      fast_scan: {
        start_hz: 100_000,
        stop_hz: 1_299_999_990,
        step_hz: 10_000,
        settle_s: 0.015,
        strategy: 'auto',
        cyclic: true,
      },
    },
  });
  for (const override of [
    { startHz: 99_990 },
    { stopHz: 1_300_000_010 },
    { stepHz: 10 },
    { stepHz: 9_999 },
    { stopHz: 400_000_001 },
    { receiverPort: 'sim://unit' },
    { checkEvents: true },
  ]) {
    expect(() => receiveSession({ ...physical, primarySource: 'ardv1', ...override })).toThrow();
  }
});

it('counts the inclusive receiver edge at full range and on a partial final step', async () => {
  const { receiverGridCount, tinyDutyPlan } = await import('../src/api');
  expect(receiverGridCount(100_000, 1_300_000_000, 10_000)).toBe(129_991);
  expect(receiverGridCount(100_000_000, 400_000_000, 10_000)).toBe(30_001);
  expect(receiverGridCount(100_000_000, 100_015_010, 10_000)).toBe(3);
  const grid = {
    ...physical,
    primarySource: 'tinysa' as const,
    tinyGrid: true,
    startHz: 100_000,
    stopHz: 1_300_000_000,
  };
  expect(tinyDutyPlan(grid).bands[0]).toMatchObject({
    start_hz: 100_000,
    stop_hz: 1_300_000_000,
    points: 129_991,
  });
  expect(receiveSession(grid).streams[0].settings).toMatchObject({
    start_hz: 100_000,
    stop_hz: 2_990_000,
    points: 290,
  });
  expect(() => tinyDutyPlan({ ...grid, stopHz: 1_299_999_990 })).toThrow(/10 kHz/);
});

it('preserves a requested tinySA single-window step when every endpoint fits the RX profile', () => {
  const f = {
    ...physical,
    primarySource: 'tinysa' as const,
    startHz: 100000000,
    stopHz: 101000000,
    stepHz: 25000,
  };
  expect(receiveSession(f).streams[0].settings).toMatchObject({
    points: 41,
    start_hz: 100000000,
    stop_hz: 101000000,
  });
  for (const invalid of [{ stopHz: 101001000 }, { stopHz: 111000000 }])
    expect(() => receiveSession({ ...f, ...invalid })).toThrow();
});
