// @vitest-environment happy-dom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { ConnectionGate } from '../src/ConnectionGate';
import { setLocale } from '../src/i18n';
import { browserConnection } from '../src/transport';

vi.mock('../src/App', () => ({
  App: ({ onConnectionSettings }: { onConnectionSettings: () => void }) =>
    createElement('button', { onClick: onConnectionSettings }, 'Settings'),
}));

let element: HTMLDivElement, root: Root;
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  setLocale('en');
  element = document.createElement('div');
  document.body.append(element);
  root = createRoot(element);
});
afterEach(async () => {
  await act(() => root.unmount());
  element.remove();
  vi.unstubAllGlobals();
});
const button = (text: string) =>
  [...element.querySelectorAll('button')].find((b) => b.textContent === text)!;
function transport(mode: 'lan_tls' | 'legacy_loopback') {
  return {
    ...browserConnection,
    native: true,
    connect: vi.fn(async () => {}),
    localNode: vi.fn(async () => {}),
    request: vi.fn(async () => ({
      contract: 'openradioinspector-platform',
      contract_version: 1,
      data: { mode, pairing_required: false },
    })),
  };
}

it('starts the bundled node on explicit local selection and stops it only on operator shutdown', async () => {
  const native = transport('legacy_loopback');
  await act(() => root.render(createElement(ConnectionGate, { transport: native })));
  expect(native.localNode).not.toHaveBeenCalled();
  await act(() => button('Connect to the local node').click());
  expect(native.localNode).toHaveBeenCalledWith('start');
  expect(native.connect).toHaveBeenCalledWith('http://127.0.0.1:8770');
  await act(() => element.querySelector<HTMLInputElement>('input[type=checkbox]')!.click());
  await act(() => button('Continue with legacy loopback core').click());
  await act(() => button('Settings').click());
  await act(() => button('Enable local node at sign-in').click());
  await act(() => button('Disable sign-in startup').click());
  await act(() => button('Stop local node and monitoring').click());
  expect(native.localNode.mock.calls).toEqual([
    ['start'],
    ['register'],
    ['unregister'],
    ['shutdown'],
  ]);
  expect(button('Connect to the local node')).toBeDefined();
});

it('connects to a remote node and closes the UI without sending local controls', async () => {
  const pick = vi.fn();
  const native = { ...transport('lan_tls'), chooseLocalArchiveDirectory: pick };
  await act(() => root.render(createElement(ConnectionGate, { transport: native })));
  await act(() => element.querySelector('form')!.requestSubmit());
  expect(native.connect).toHaveBeenCalledWith('https://radio.local:8770');
  expect(button('Settings')).toBeDefined();
  await act(() => button('Settings').click());
  expect(button('Choose local archive folder')).toBeUndefined();
  expect(pick).not.toHaveBeenCalled();
  await act(() => root.render(null));
  expect(native.localNode).not.toHaveBeenCalled();
});

it('keeps local monitoring active when its window unmounts', async () => {
  const native = transport('legacy_loopback');
  await act(() => root.render(createElement(ConnectionGate, { transport: native })));
  await act(() => button('Connect to the local node').click());
  await act(() => root.render(null));
  expect(native.localNode.mock.calls).toEqual([['start']]);
});

it('chooses a local archive before startup, preserves the selection on cancellation and requires explicit shutdown before moving it', async () => {
  const pick = vi
    .fn<() => Promise<string | null>>()
    .mockResolvedValueOnce('D:/OpenRadioInspector Archive')
    .mockResolvedValueOnce(null);
  const native = { ...transport('legacy_loopback'), chooseLocalArchiveDirectory: pick };
  await act(() => root.render(createElement(ConnectionGate, { transport: native })));
  expect(button('Choose local archive folder')).toBeDefined();
  await act(() => button('Choose local archive folder').click());
  expect(element.textContent).toContain('D:/OpenRadioInspector Archive');
  await act(() => button('Choose local archive folder').click());
  expect(element.textContent).toContain('D:/OpenRadioInspector Archive');
  expect(native.localNode).not.toHaveBeenCalled();
  await act(() => button('Connect to the local node').click());
  await act(() => element.querySelector<HTMLInputElement>('input[type=checkbox]')!.click());
  await act(() => button('Continue with legacy loopback core').click());
  await act(() => button('Settings').click());
  expect(button('Choose local archive folder')).toBeUndefined();
  await act(() => button('Stop local node and monitoring').click());
  expect(button('Choose local archive folder')).toBeDefined();
});
