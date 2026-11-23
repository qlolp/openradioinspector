// @vitest-environment happy-dom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { FrequencyBookmarkSet } from '../../../contracts/generated/typescript/index';
import { Api, type Transport } from '../src/api';
import { RfPassMarkers } from '../src/RfPassMarkers';
import { setLocale } from '../src/i18n';
import type { PassPoint } from '../src/rf-pass';

const session = 'survey/a';
const path = '/api/v1/sessions/survey%2Fa/frequency-bookmarks';
const first = 100_000_000,
  second = 100_010_000;
const recorded: FrequencyBookmarkSet = {
  id: 'set/3',
  session_id: session,
  schema_version: 1,
  name: 'Harbor',
  revision: 3,
  created_at: '2026-11-23T10:00:00.000000Z',
  entries: [
    { label: 'Beacon', frequency_hz: first },
    { label: 'Tower', frequency_hz: second },
  ],
};
const response = (data: unknown) => ({
  contract: 'openradioinspector-platform',
  contract_version: 1,
  data,
});
const page = (...items: FrequencyBookmarkSet[]) =>
  response({ session_id: session, limit: 256, items });

let host: HTMLDivElement, root: Root, api: Api;
let send: ReturnType<typeof vi.fn<Transport['request']>>;
const select = vi.fn<(hz: number) => void>();
beforeEach(() => {
  setLocale('en');
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
  send = vi.fn<Transport['request']>().mockResolvedValue(page());
  api = new Api({ request: send });
  select.mockClear();
});
afterEach(async () => {
  await act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});
async function show(frequency: number | null, readOnly = false) {
  const point: PassPoint | null =
    frequency === null
      ? null
      : {
          frequency_hz: frequency,
          level_dbm: -82,
          sweep_id: 'window-1',
          tile: 1,
          point_index: (frequency - first) / 10_000,
          started_at: recorded.created_at,
          ended_at: recorded.created_at,
          last_measured_at: recorded.created_at,
        };
  await act(() =>
    root.render(createElement(RfPassMarkers, { api, session, point, readOnly, select })),
  );
}
function button(text: string) {
  const found = Array.from(host.querySelectorAll('button')).find(
    (el) => (el.getAttribute('aria-label') ?? el.textContent) === text,
  );
  expect(found, text).toBeDefined();
  return found!;
}
async function click(text: string) {
  await act(() => button(text).click());
}
function input(label: string) {
  const field = Array.from(host.querySelectorAll('label'))
    .find((el) => el.textContent === label)
    ?.querySelector('input');
  expect(field, label).toBeDefined();
  return field!;
}
async function edit(label: string, value: string) {
  const field = input(label);
  await act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value);
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
async function recall(id: string) {
  const control = host.querySelector('select')!;
  await act(() => {
    control.value = id;
    control.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

it('saves a named set from selected source points and recalls each exact frequency', async () => {
  await show(first);
  await click('Add selected frequency');
  expect(button('Add selected frequency').disabled).toBe(true);
  await edit('Marker set name', ' Harbor ');
  await edit('Label for 100,000,000 Hz', 'Beacon');
  await show(second);
  await click('Add selected frequency');
  await edit('Label for 100,010,000 Hz', ' Tower ');
  send.mockResolvedValueOnce(response(recorded)).mockResolvedValue(page(recorded));
  await click('Save marker set');
  expect(
    send.mock.calls.filter(([, body]) => body !== undefined).map((call) => call.slice(0, 2)),
  ).toEqual([
    [
      path,
      {
        name: 'Harbor',
        entries: [
          { label: 'Beacon', frequency_hz: first },
          { label: 'Tower', frequency_hz: second },
        ],
      },
    ],
  ]);
  expect(host.querySelectorAll('option')).toHaveLength(3);
  await recall('set/3/0');
  await recall('set/3/1');
  expect(select.mock.calls).toEqual([[first], [second]]);
});

it('edits a saved revision without losing the original set or requiring a live point', async () => {
  send.mockResolvedValue(page(recorded));
  await show(null);
  await recall('set/3/1');
  await click('Edit marker set');
  expect(input('Marker set name').value).toBe('Harbor');
  expect(input('Label for 100,000,000 Hz').value).toBe('Beacon');
  await click('Remove 100,000,000 Hz');
  await edit('Label for 100,010,000 Hz', '');
  expect(button('Save marker set').disabled).toBe(true);
  await edit('Label for 100,010,000 Hz', 'Tower east');
  const newer = {
    ...recorded,
    id: 'set/4',
    revision: 4,
    entries: [{ label: 'Tower east', frequency_hz: second }],
  };
  send.mockResolvedValueOnce(response(newer)).mockResolvedValue(page(newer, recorded));
  await click('Save marker set');
  expect(send.mock.calls.find(([, body]) => body !== undefined)?.slice(0, 2)).toEqual([
    path,
    { name: 'Harbor', entries: [{ label: 'Tower east', frequency_hz: second }] },
  ]);
  expect(host.textContent).toContain('Revision 4');
  expect(host.textContent).toContain('Revision 3');
});

it('blocks a fifth frequency and keeps an emptied draft from saving the current point', async () => {
  const frequencies = [first, second, 100_020_000, 100_030_000];
  for (const frequency of frequencies) {
    await show(frequency);
    await click('Add selected frequency');
  }
  await show(100_040_000);
  expect(button('Add selected frequency').disabled).toBe(true);
  await edit('Marker set name', 'Route');
  for (const frequency of frequencies)
    await click('Remove ' + frequency.toLocaleString('en-US') + ' Hz');
  expect(button('Save marker set').disabled).toBe(true);
  await click('Save marker set');
  expect(send.mock.calls.some(([, body]) => body !== undefined)).toBe(false);
  expect(button('Add selected frequency').disabled).toBe(false);
});

it('preserves the one-marker shortcut and offers readers only saved frequencies', async () => {
  await show(first);
  await edit('Marker name', 'Beacon');
  const single = { ...recorded, name: 'Beacon', entries: [recorded.entries[0]] };
  send.mockResolvedValueOnce(response(single)).mockResolvedValue(page(single));
  await click('Save marker');
  expect(send.mock.calls.find(([, body]) => body !== undefined)?.slice(0, 2)).toEqual([
    path,
    { name: 'Beacon', entries: [{ label: 'Beacon', frequency_hz: first }] },
  ]);
  await show(second, true);
  expect(host.querySelectorAll('input, button')).toHaveLength(0);
  await recall('set/3/0');
  expect(select).toHaveBeenCalledWith(first);
});
