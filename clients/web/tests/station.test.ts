import { expect, it } from 'vitest';
import { formatBytes, measuredRate, recoveryNotices } from '../src/station';
import { gridPointCount } from '../src/scan-plan';
import { waterfallPixels } from '../src/spectrum-view';
import type { DisplaySweep, GuardStatus, WorkerStatus } from '../src/types';

it('uses the lifetime and committed measurement count of a live worker for its average', () => {
  const source = {
    alive: true,
    started_at: '2026-01-01T00:00:01Z',
    last_measurement_at: '2026-01-01T00:00:05Z',
    measurements: 8,
  } as WorkerStatus;
  expect(measuredRate(source)).toBe(2);
  expect(measuredRate({ ...source, alive: false })).toBeUndefined();
  expect(measuredRate({ ...source, started_at: source.last_measurement_at })).toBeUndefined();
  expect(measuredRate(undefined)).toBeUndefined();
  expect(formatBytes(1024 * 1024)).toContain('1');
  expect(formatBytes(undefined)).not.toMatch(/0/);
});

it('distinguishes archive, paused coverage and unavailable USB roles', () => {
  const guard = {
    active: true,
    manual: [],
    receiver_busy: false,
    audio_configured: false,
    hardware_validation: 'not_run',
    project: {
      id: 'p',
      name: 'p',
      site: 'test',
      state: 'degraded',
      active_session_id: 's',
      baseline_id: null,
      coverage: 'wide',
      analyzer_stream_id: 'a',
      receiver_stream_id: 'r',
    },
    workers: [
      {
        stream_id: 'r',
        device: 'AR-DV1',
        current: {},
        measurements: 0,
        adapter: 'ardv1-serial',
        state: 'error',
        alive: false,
        last_error: 'port unplugged',
      },
    ],
  } as GuardStatus;
  expect(recoveryNotices(guard, { ready: false }).map((item) => item.kind)).toEqual([
    'archive',
    'receiver',
  ]);
  expect(
    recoveryNotices({ ...guard, project: { ...guard.project, coverage: 'narrow' } }, undefined).map(
      (item) => item.kind,
    ),
  ).toContain('pause');
});

it('counts inclusive grid points and preserves a narrow peak and unmeasured gaps on a band axis', () => {
  expect(gridPointCount('100000', '1300000000')).toBe(129991);
  expect(gridPointCount('100000', '131000')).toBeUndefined();
  expect(gridPointCount('100000', '100000')).toBeUndefined();
  const sweep = (index: number, frequency: number[], levels: number[]) =>
    ({
      scan_context: { plan_id: 'plan', band_id: 'band', window_index: index, window_count: 2 },
      frequencies_hz: frequency,
      levels,
      level_unit: 'dBm',
    }) as DisplaySweep;
  const recent = sweep(0, [0, 2, 3], [-60, -30, -70]);
  const pixels = waterfallPixels(recent, 10, { low: 0, high: 100 });
  expect(pixels[0]).toBeGreaterThan(0);
  expect(pixels[4]).toBe(0);
});

it('bounds a finite receiver segment and counts its inclusive last frequency', async () => {
  const { segmentPoints } = await import('../src/ReceiverSegment');
  expect(segmentPoints(100000000, 100100010, 25000)).toBe(6);
  expect(segmentPoints(100000, 110000, 10)).toBeUndefined();
  expect(segmentPoints(100000, 109990, 10)).toBe(1000);
  expect(segmentPoints(100005, 110005, 50)).toBeUndefined();
  expect(segmentPoints(100000, 110000, 0)).toBeUndefined();
});

it('prepares receiver readback only when requested for a serial session', async () => {
  const { receiveSession } = await import('../src/api');
  const form = {
    name: 'Receive',
    simulator: false,
    analyzerPort: 'A',
    receiverPort: 'R',
    startHz: 88000000,
    stopHz: 108000000,
    points: 201,
    rbwHz: 10000,
  };
  expect(receiveSession(form).streams[1].settings).not.toHaveProperty('monitoring_rx_readback');
  expect(receiveSession({ ...form, monitoringReadback: true }).streams[1].settings).toHaveProperty(
    'monitoring_rx_readback',
    true,
  );
  expect(
    receiveSession({ ...form, monitoringReadback: true, simulator: true }).streams[1].settings,
  ).not.toHaveProperty('monitoring_rx_readback');
});
