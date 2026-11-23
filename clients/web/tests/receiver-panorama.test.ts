import { expect, it } from 'vitest';
import { responseValue } from '../src/wire';
const path = '/api/v1/sessions/session/streams/receiver/receiver-panorama';
const pass = {
  pass: 3,
  total_points: 4,
  measured_points: 2,
  complete: false,
  start_hz: 100000000,
  stop_hz: 100025010,
  step_hz: 10000,
  level_unit: 's_meter_count',
  lower_is_stronger: true,
  coverage_hex: '09',
  started_at: '2026-11-20T00:00:00Z',
  ended_at: '2026-11-20T00:00:02Z',
  bins: [
    {
      start_index: 0,
      end_index: 1,
      measured_points: 1,
      frequency_hz: 100000000,
      level: 26.5,
      observation_id: 'first',
      started_at: '2026-11-20T00:00:00Z',
      ended_at: '2026-11-20T00:00:01Z',
    },
    {
      start_index: 2,
      end_index: 3,
      measured_points: 1,
      frequency_hz: 100025010,
      level: 4.5,
      observation_id: 'last',
      started_at: '2026-11-20T00:00:01Z',
      ended_at: '2026-11-20T00:00:02Z',
    },
  ],
};
const read = (data: unknown) =>
  (responseValue(path + '?columns=2', JSON.stringify({ data })) as { data: typeof pass }).data;
it('decodes bounded original S-meter levels and admits a partial bin with its off-grid final original', () => {
  expect(read(pass).bins.map((b) => b.level)).toEqual([26.5, 4.5]);
});
it('pins a stored event to its own receiver context even when pass numbers coincide', () => {
  const context = 'a'.repeat(64);
  const url = path + '?pass=3&columns=2&context_id=' + context;
  const open = (data: unknown) => responseValue(url, JSON.stringify({ data }));
  expect(() => open({ ...pass, context_id: context })).not.toThrow();
  expect(() => open({ ...pass, context_id: 'b'.repeat(64) })).toThrow();
  expect(() => open(pass)).toThrow();
});
it('rejects falsely complete coverage or a reported original outside its bin', () => {
  for (const invalid of [
    { ...pass, complete: true },
    { ...pass, level_unit: 'dBm' },
    { ...pass, bins: [{ ...pass.bins[0], frequency_hz: 100025010 }, pass.bins[1]] },
  ])
    expect(() => read(invalid)).toThrow();
});

it('refuses coverage bits that disagree with an original point, even when the count agrees', () => {
  expect(() => read({ ...pass, coverage_hex: '0a' })).toThrow();
  expect(() => read({ ...pass, coverage_hex: '89' })).toThrow();
});
