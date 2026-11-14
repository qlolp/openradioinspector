// @vitest-environment happy-dom
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Api } from '../src/api';
import { AudioEvidence } from '../src/AudioEvidence';
import { setLocale } from '../src/i18n';

const envelope = (data: unknown) => ({
  contract: 'openradioinspector-platform',
  contract_version: 1,
  data,
});
let root: Root, element: HTMLDivElement, api: Api;
let calls: { path: string; body?: unknown; signal?: AbortSignal }[];
const audio = vi.fn(async () => new Blob(['fixture-wav'], { type: 'audio/wav' }));
async function render(projectId = 'p/1', eventId = 'e/1', available = true) {
  await act(() =>
    root.render(createElement(AudioEvidence, { api, projectId, eventId, available, audio })),
  );
}
async function input(selector: string, value: string) {
  await act(() => {
    const field = element.querySelector(selector) as HTMLInputElement;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value);
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.dispatchEvent(new Event('change', { bubbles: true }));
  });
}
beforeEach(() => {
  setLocale('en');
  calls = [];
  audio.mockClear();
  element = document.createElement('div');
  document.body.append(element);
  root = createRoot(element);
  api = new Api({
    request: async (path, body, signal) => {
      calls.push({ path, body, signal });
      if (path.endsWith('/info'))
        return envelope({
          artifact_id: 'source',
          sha256: 'a'.repeat(64),
          frame_count: 8000,
          sample_rate_hz: 8000,
        });
      if (body) return envelope({ id: 'mark', label: 'Voice', start_frame: 800, end_frame: 1600 });
      return envelope({ items: [], next_cursor: null });
    },
  });
});
afterEach(async () => {
  await act(() => root.unmount());
  element.remove();
  vi.restoreAllMocks();
});

it('saves operator marks against the exact event and frame range without any capture request', async () => {
  await render();
  await input('[name="start"]', '800');
  await input('[name="end"]', '1600');
  await input('[name="label"]', 'Voice');
  await act(() =>
    element
      .querySelector('form')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })),
  );
  expect(calls.find((call) => call.body)).toMatchObject({
    path: '/api/v1/monitoring-projects/p%2F1/events/e%2F1/audio/marks',
    body: {
      start_frame: 800,
      end_frame: 1600,
      label: 'Voice',
      source_artifact_id: 'source',
      source_sha256: 'a'.repeat(64),
    },
  });
  expect(calls.every((call) => /\/audio\/(info|marks)(\?|$)/.test(call.path))).toBe(true);
  expect(audio).not.toHaveBeenCalled();
});

it('exports a bounded WAV clip and revokes its temporary download URL', async () => {
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:fixture');
  const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  await render();
  await act(() => (element.querySelector('[data-export-audio]') as HTMLButtonElement).click());
  expect(audio).toHaveBeenCalledWith(
    '/api/v1/monitoring-projects/p%2F1/events/e%2F1/audio/clip?start_frame=0&end_frame=8000&source_artifact_id=source&source_sha256=' +
      'a'.repeat(64),
    expect.any(AbortSignal),
  );
  expect(click).toHaveBeenCalledOnce();
  expect(revoke).toHaveBeenCalledWith('blob:fixture');
});

it('does not access an absent capability and aborts stale archive requests after an event change', async () => {
  await render('p', 'e', false);
  expect(calls).toHaveLength(0);
  let release!: (value: unknown) => void;
  api = new Api({
    request: (path, body, signal) => {
      calls.push({ path, body, signal });
      return new Promise((resolve) => {
        release = resolve;
      });
    },
  });
  await render('p', 'old');
  const pending = calls[0].signal!;
  await render('p', 'new', false);
  expect(pending.aborted).toBe(true);
  await act(async () => {
    release(envelope({ frame_count: 123, sample_rate_hz: 8000 }));
  });
  expect(element.querySelector('[data-export-audio]')).toBeNull();
});
