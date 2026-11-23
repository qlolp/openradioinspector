// @vitest-environment happy-dom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Api, type Transport } from '../src/api';
import { RfExplorerResearch } from '../src/RfExplorerResearch';
import { setLocale } from '../src/i18n';
import { parseWire, stringifyJSON } from '../src/wire';

const frequencies = Array.from({ length: 16 }, (_, i) => 15_000_000 + i * 10_000);
const source = {
  id: 'window-2',
  session_id: 's/a',
  stream_id: 'rfe',
  interval: { started_at: '2026-11-23T10:00:00.000000Z', ended_at: '2026-11-23T10:00:01.000000Z' },
  frequencies_hz: frequencies,
  levels: frequencies.map(() => -76),
  level_unit: 'dBm',
  settings: {
    adapter: 'rf-explorer-3g-serial',
    receive_profile: 'rf-explorer-3g-combo-standard-1.34',
    device_identity: { main_model: 3, expansion_model: 5, firmware: '01.34' },
    device_mode: 'spectrum_analyzer',
    settings_readback: true,
    settings_source: 'device_readback',
    timing_source: 'host_frame_read',
    simulated: true,
    active_board: 'expansion',
    active_input: 'right',
    board_min_hz: 15_000_000,
    board_max_hz: 2_700_000_000,
    board_max_span_hz: 600_000_000,
    start_hz: frequencies[0],
    stop_hz: frequencies[15],
    step_hz: 10_000,
    points: frequencies.length,
    rbw_hz: 15_000,
    amplitude_bottom_dbm: -120,
    amplitude_top_dbm: -10,
    offset_db: 0,
    level_encoding: 'dBm = offset_db - u8 / 2',
    detector_code: 0,
    detector: 'normal',
    artifact_kind: 'raw_spectrum',
  },
};
const features = { rf_explorer_band_power: true, sweep_tolerance_envelope: true };
const band = {
  source,
  value: -66,
  unit: 'dBm',
  start_hz: 15000000,
  stop_hz: 15150000,
  start_index: 0,
  stop_index: 15,
  rbw_hz: 15000,
  integration: 'trapezoid_over_actual_grid',
  enbw_correction: 'not_calibrated',
  hardware_validation: 'not_run',
  uncertainty_db: null,
};
let element: HTMLDivElement;
let root: Root;
let send: ReturnType<typeof vi.fn<Transport['request']>>;
let api: Api;

beforeEach(() => {
  setLocale('en');
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  element = document.createElement('div');
  document.body.append(element);
  root = createRoot(element);
  send = vi.fn();
  api = new Api({ request: (path, body, signal) => send(path, body, signal) });
});
afterEach(async () => {
  await act(() => root.unmount());
  element.remove();
  vi.unstubAllGlobals();
});
const response = (data: unknown) => ({
  contract: 'openradioinspector-platform',
  contract_version: 1,
  data: parseWire(JSON.stringify(data)),
});
const render = (available: Record<string, unknown> = features) =>
  act(() => root.render(createElement(RfExplorerResearch, { api, source, features: available })));
const submit = (index: number) =>
  act(async () => {
    element
      .querySelectorAll('form')
      [index].dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
async function edit(input: HTMLInputElement, value: string) {
  await act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

it('uses the archived ID and actual endpoints for band power, and shows units and uncertainty', async () => {
  send.mockResolvedValue(response(band));
  await render({ rf_explorer_band_power: true });
  await submit(0);
  expect(send.mock.calls[0].slice(0, 2)).toEqual([
    '/api/v1/sessions/s%2Fa/rf-explorer-band-power',
    { sweep_id: 'window-2', start_hz: 15000000, stop_hz: 15150000 },
  ]);
  expect(element.textContent).toContain('-66.00 dBm');
  expect(element.textContent).toContain('15,000 Hz');
  expect(element.textContent).toContain('Uncertainty unknown');
  send.mockResolvedValue(response({ ...band, source: { ...source, id: 'different-window' } }));
  await submit(0);
  expect(element.querySelector('[role="alert"]')?.textContent).toContain(
    'Source evidence mismatch',
  );
  expect(element.textContent).not.toContain('-66.00 dBm');
});

it('uses the chosen reference, displays both limits and preserves uncovered bins', async () => {
  const reference = {
    ...source,
    id: 'ref',
    interval: {
      started_at: '2026-11-23T09:59:58.000000Z',
      ended_at: '2026-11-23T09:59:59.000000Z',
    },
    frequencies_hz: frequencies.map((f) => f + 10000),
    levels: [-81, ...source.levels.slice(1)],
    settings: { ...source.settings, start_hz: 15010000, stop_hz: 15160000 },
  };
  const result = {
    session_id: source.session_id,
    stream_id: source.stream_id,
    reference,
    target: source,
    level_unit: 'dBm',
    delta_unit: 'dB',
    upper_deviation_db: 4,
    lower_deviation_db: 2,
    counts: { above: 1, below: 0, within: 14, uncovered: 1 },
    bins: frequencies.map((frequency, i) => {
      const baseline = reference.levels[i - 1];
      return {
        target_index: i,
        reference_index: i === 0 ? null : i - 1,
        frequency_hz: frequency,
        lower_dbm: i === 0 ? null : baseline - 2,
        upper_dbm: i === 0 ? null : baseline + 4,
        delta_db: i === 0 ? null : source.levels[i] - baseline,
        classification: i === 0 ? 'uncovered' : i === 1 ? 'above' : 'within',
      };
    }),
  };
  send.mockResolvedValue(response(result));
  await render({ sweep_tolerance_envelope: true });
  await edit(element.querySelector('input[name="reference"]')!, 'ref');
  await edit(element.querySelector('input[name="upper"]')!, '4');
  await edit(element.querySelector('input[name="lower"]')!, '2');
  await submit(0);
  const [path, body] = send.mock.calls.at(-1)!;
  expect([path, JSON.parse(stringifyJSON(body))]).toEqual([
    '/api/v1/sessions/s%2Fa/sweep-tolerance-envelope',
    { target_id: 'window-2', reference_id: 'ref', upper_deviation_db: 4, lower_deviation_db: 2 },
  ]);
  expect(element.textContent).toContain('Uncovered');
  const plot = element.querySelector('[aria-label="Measured levels and reference limits"]')!;
  expect(plot.querySelectorAll('[data-tolerance-limit]')).toHaveLength(15);
  expect(plot.querySelectorAll('[data-measured-point]')).toHaveLength(16);
  expect(element.querySelector('tbody')!.textContent).toContain('-83');
  expect(element.querySelector('tbody')!.textContent).toContain('-77');
  await edit(element.querySelector('input[name="upper"]')!, '6');
  expect(element.querySelector('tbody')).toBeNull();
});

it('drops late results when source inputs change and hides unavailable operations', async () => {
  let finish!: (value: unknown) => void;
  send.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  await render({ rf_explorer_band_power: true });
  await submit(0);
  await edit(element.querySelector('input[name="stop"]')!, '15000000');
  expect(send.mock.calls[0][2]?.aborted).toBe(true);
  await act(async () => finish(response(band)));
  expect(element.textContent).not.toContain('-66.00 dBm');
  await render({});
  expect(element.querySelector('form')).toBeNull();
});
