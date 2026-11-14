// @vitest-environment happy-dom
import { readFileSync } from 'node:fs';
import { resolve as resolvePath } from 'node:path';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Api, type Transport } from '../src/api';
import { FieldStrengthPanel } from '../src/FieldStrength';
import { setLocale } from '../src/i18n';
import { parseWire, stringifyJSON } from '../src/wire';

const fixture = JSON.parse(
  readFileSync(
    resolvePath(process.cwd(), '../../tests/fixtures/field-strength-rms-v2.json'),
    'utf8',
  ),
);
const source = {
  id: fixture.source.id,
  session_id: 'session',
  stream_id: 'stream',
  peak: { frequency_hz: 200 },
};
const result = () => parseWire(JSON.stringify(fixture.result));
const envelope = (data: unknown) => ({
  contract: 'openradioinspector-platform',
  contract_version: 1,
  data,
});
let element: HTMLDivElement;
let root: Root;
let calls: { path: string; body: unknown; signal?: AbortSignal }[];
let resolve: (value: unknown) => void;
let api: Api;

beforeEach(() => {
  setLocale('en');
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  element = document.createElement('div');
  document.body.append(element);
  root = createRoot(element);
  calls = [];
  const transport: Transport = {
    request(path, body, signal) {
      calls.push({ path, body, signal });
      return new Promise((yes) => {
        resolve = yes;
      });
    },
  };
  api = new Api(transport);
});
afterEach(async () => {
  await act(() => root.unmount());
  element.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
async function render(overrides: Partial<Parameters<typeof FieldStrengthPanel>[0]> = {}) {
  await act(() =>
    root.render(
      createElement(FieldStrengthPanel, {
        api,
        source,
        available: true,
        connectionVersion: 1,
        ...overrides,
      }),
    ),
  );
}
async function edit(value: string) {
  const editor = element.querySelector('textarea')!;
  await act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(
      editor,
      value,
    );
    editor.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
async function submit() {
  await act(() => {
    element
      .querySelector('form')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
}
async function complete(value: unknown = result()) {
  await act(async () => {
    resolve(envelope(value));
    await Promise.resolve();
  });
}

it('uses the shared strict contract and the original source, without deriving calibration', async () => {
  await render();
  expect(element.textContent).toContain('Operator-supplied calibration');
  expect((element.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(true);
  await edit(JSON.stringify(fixture.request.calibration));
  await submit();
  expect(calls).toHaveLength(1);
  expect(calls[0].path).toBe('/api/v1/sessions/session/field-strength-rms-v2');
  expect(JSON.parse(stringifyJSON(calls[0].body))).toEqual({
    ...fixture.request,
    calibration: {
      ...fixture.request.calibration,
      valid_from: '2020-01-01T00:00:00.000000Z',
      valid_until: '2030-01-01T00:00:00.000000Z',
    },
  });
  await complete();
  expect(element.textContent).toContain('55.000');
  expect(element.textContent).toContain('3 dB');
  expect(element.textContent).toContain(fixture.result.calibration.sha256);
  expect(element.textContent).toContain(source.id);
});

it('preserves a finite large integral double through request encoding and profile evidence comparison', async () => {
  await render();
  const profile = JSON.parse(JSON.stringify(fixture.request.calibration));
  for (const point of profile.points) point.uncertainty_db = 1e16;
  await edit(JSON.stringify(profile));
  await submit();
  expect(calls).toHaveLength(1);
  expect(JSON.parse(stringifyJSON(calls[0].body)).calibration.points[0].uncertainty_db).toBe(1e16);
  const value = JSON.parse(JSON.stringify(fixture.result));
  value.calibration.profile = profile;
  value.uncertainty_db = value.calibration.coefficients.uncertainty_db = 1e16;
  await complete(parseWire(JSON.stringify(value)));
  expect(element.querySelector('[data-testid="field-strength-result"]')).not.toBeNull();
  expect(element.textContent).toContain('10000000000000000 dB');
});

it('keeps unknown uncertainty distinct from zero and exposes core refusal', async () => {
  await render();
  await edit(JSON.stringify(fixture.request.calibration));
  await submit();
  const value = JSON.parse(JSON.stringify(fixture.result));
  value.uncertainty_db = null;
  await complete(parseWire(JSON.stringify(value)));
  expect(element.textContent).toContain('Uncertainty unknown');
  await submit();
  await act(async () => {
    resolve({
      contract: 'openradioinspector-platform',
      contract_version: 1,
      error: { code: 'field_strength_unavailable', message: 'Incompatible measurement chain' },
    });
    await Promise.resolve();
  });
  expect(element.textContent).toContain('Incompatible measurement chain');
  expect(element.querySelector('[data-testid="field-strength-result"]')).toBeNull();
});

it.each(['duplicate', 'nonfinite', 'oversized', 'utf8_budget'])(
  'refuses %s calibration before sending a request',
  async (kind) => {
    await render();
    let raw = JSON.stringify(fixture.request.calibration);
    if (kind === 'duplicate') raw = raw.replace('"version":1', '"version":1,"version":2');
    if (kind === 'nonfinite') raw = raw.replace('"uncertainty_db":2', '"uncertainty_db":1e999');
    if (kind === 'oversized') raw += ' '.repeat(65536);
    if (kind === 'utf8_budget')
      raw = JSON.stringify({
        ...fixture.request.calibration,
        uncertainty_basis: 'Ж'.repeat(40000),
      });
    await edit(raw);
    await submit();
    expect(calls).toHaveLength(0);
    expect(element.querySelector('[role="alert"]')?.textContent?.length).toBeGreaterThan(0);
  },
);

it.each(['source', 'connection', 'draft'])(
  'aborts on %s change and refuses a late reply',
  async (kind) => {
    await render();
    await edit(JSON.stringify(fixture.request.calibration));
    await submit();
    const signal = calls[0].signal;
    if (kind === 'source') await render({ source: { ...source, id: 'another-original' } });
    if (kind === 'connection') await render({ connectionVersion: 2 });
    if (kind === 'draft') await edit('{}');
    expect(signal?.aborted).toBe(true);
    await complete();
    expect(element.querySelector('[data-testid="field-strength-result"]')).toBeNull();
  },
);

it.each([
  'id',
  'stream_id',
  'session_id',
  'frequency_hz',
  'coefficient_frequency',
  'calibration',
  'sha256',
])('rejects a reply for another source %s', async (field) => {
  await render();
  await edit(JSON.stringify(fixture.request.calibration));
  await submit();
  const value = JSON.parse(JSON.stringify(fixture.result));
  if (field === 'coefficient_frequency') value.calibration.coefficients.frequency_hz = 201;
  else if (field === 'calibration')
    value.calibration.profile.measurement_chain_id = 'another-tract';
  else if (field === 'sha256') value.calibration.sha256 = 'invalid';
  else value.source[field] = field === 'frequency_hz' ? 201 : 'different';
  await complete(parseWire(JSON.stringify(value)));
  expect(element.querySelector('[data-testid="field-strength-result"]')).toBeNull();
  expect(element.querySelector('[role="alert"]')?.textContent).toContain(
    'Source evidence mismatch',
  );
});

it('has a total deadline even if transport ignores cancellation, and respects old capabilities', async () => {
  vi.useFakeTimers();
  await render();
  await edit(JSON.stringify(fixture.request.calibration));
  await submit();
  await act(() => vi.advanceTimersByTimeAsync(6000));
  expect(calls[0].signal?.aborted).toBe(true);
  expect(element.textContent).toContain('Analysis deadline exceeded');
  await complete();
  expect(element.querySelector('[data-testid="field-strength-result"]')).toBeNull();
  await render({ available: false });
  expect(element.textContent).toContain('Unavailable on this core');
  expect((element.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(true);
});
