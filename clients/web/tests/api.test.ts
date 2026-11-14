import { describe, expect, it } from 'vitest';
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
