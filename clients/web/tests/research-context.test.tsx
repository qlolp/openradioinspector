// @vitest-environment happy-dom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { Research } from '../src/Research';
import { setLocale } from '../src/i18n';
import { useDesk } from '../src/useDesk';
import type { Connection } from '../src/transport';
import type { Comparison, MonitoringEvent, Project, Sweep } from '../src/types';

// Graphics need a real browser; state, hooks, API envelopes and response ordering
// below are real. Assertions target Research/App DOM, never this drawing stub.
vi.mock('../src/charts', () => ({ Spectrum: () => null, Waterfall: () => null }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => (resolve = yes));
  return { promise, resolve };
}
const envelope = (data: unknown) => ({
  contract: 'openradioinspector-platform',
  contract_version: 1,
  data,
});
const sweep = (id: string): Sweep => ({
  id,
  session_id: 'session',
  stream_id: 'analyzer',
  interval: {
    started_at: '2026-11-11T00:00:00.123456Z',
    ended_at: '2026-11-11T00:00:01.123456Z',
  },
  frequencies_hz: [100_000_000, 100_001_000],
  levels: [-90, -70],
  level_unit: 'dBm',
  settings: {},
});
const project: Project = {
  id: 'project',
  name: 'Owned component fixture',
  site: 'Synthetic',
  state: 'draft',
  active_session_id: 'session',
  analyzer_stream_id: 'analyzer',
  receiver_stream_id: 'receiver',
  baseline_id: 'baseline',
};
const otherProject: Project = {
  ...project,
  id: 'other',
  name: 'Other owned project',
  active_session_id: 'other-session',
};
function ProjectHost({ transport }: { transport: Connection }) {
  const desk = useDesk(transport);
  return (
    <>
      <button onClick={() => desk.setProjectId(otherProject.id)}>Select other project</button>
      <Research desk={desk} openOriginal={() => {}} />
    </>
  );
}
function comparison(id: string): Comparison {
  return {
    compatible: true,
    baseline_id: 'baseline',
    sweep_id: id,
    items: [
      {
        frequency_hz: 100_000_000,
        current_dbm: -90,
        baseline_p50_dbm: -95,
        baseline_p95_dbm: -91,
        delta_db: 5,
      },
      {
        frequency_hz: 100_001_000,
        current_dbm: -70,
        baseline_p50_dbm: -90,
        baseline_p95_dbm: -85,
        delta_db: 20,
      },
    ],
  };
}
class CoreFixture {
  highWater = 100;
  restored = false;
  comparison = deferred<Comparison>();
  comparisonSignal?: AbortSignal;
  heldHistory?: ReturnType<typeof deferred<unknown>>;
  historySignal?: AbortSignal;
  heldOriginal?: ReturnType<typeof deferred<unknown>>;
  originalSignal?: AbortSignal;
  heldSources?: ReturnType<typeof deferred<unknown>>;
  sourcesSignal?: AbortSignal;
  heldReview?: ReturnType<typeof deferred<unknown>>;
  reviewSignal?: AbortSignal;
  state?: Parameters<Connection['subscribe']>[2];
  requests: { path: string; body: unknown }[] = [];
  event(): MonitoringEvent {
    return {
      id: 'event',
      project_id: project.id,
      kind: 'new_signal',
      state: 'confirmed',
      first_seen: '2026-11-11T00:00:00.123456Z',
      last_seen: '2026-11-11T00:00:01.123456Z',
      center_frequency_hz: 100_001_000,
      start_frequency_hz: 100_000_000,
      end_frequency_hz: 100_001_000,
      peak_level: -70,
      source_count: 1,
      disposition: 'unreviewed',
      source_refs: this.heldSources
        ? undefined
        : [
            {
              sweep_id: this.restored ? 'fresh' : 'A',
              session_id: 'session',
              stream_id: 'analyzer',
            },
          ],
    };
  }
  transport: Connection = {
    native: false,
    connect: async () => {},
    pair: async () => {},
    revokePairing: async () => {},
    openLegacy: async () => {},
    audio: async () => {
      throw Error('Unexpected audio');
    },
    subscribe: async (_after, _receive, state) => {
      this.state = state;
      return () => {};
    },
    request: async (path, body, signal) => {
      this.requests.push({ path, body });
      const url = new URL(path, 'http://owned.invalid');
      const route = url.pathname.replace('/api/v1', '');
      if (route.endsWith('/events/event/review') && this.heldReview) {
        this.reviewSignal = signal;
        return envelope(await this.heldReview.promise);
      }
      if (body !== undefined) throw Error('No device/service commands are allowed in this fixture');
      if (route === '/health')
        return envelope({ status: 'ok', last_event_id: this.highWater, sessions: 1 });
      if (route === '/capabilities')
        return envelope({ features: {}, limitations: [], hardware_validated: false });
      if (route === '/sessions')
        return envelope([{ id: 'session', name: 'Synthetic', state: 'running', streams: [] }]);
      if (route === '/monitoring-projects')
        return envelope({ items: [project, otherProject], next_cursor: null });
      if (route.endsWith('/status') && !route.endsWith('/search/status'))
        return envelope({
          project: route.includes('/other/') ? otherProject : project,
          active: false,
          receiver_busy: false,
          audio_configured: false,
          manual: [],
          workers: [],
          hardware_validation: 'not_run',
        });
      if (route.endsWith('/preflight'))
        return envelope({
          ready: false,
          checks: [],
          baseline_ready: false,
          baseline_collection: {
            sweeps: 0,
            elapsed_seconds: 0,
            minimum_sweeps: 20,
            minimum_seconds: 30,
            ready: false,
          },
          hardware_validation: 'not_run',
        });
      if (route.endsWith('/events')) return envelope({ items: [this.event()], next_cursor: null });
      if (route.endsWith('/events/event')) return envelope(this.event());
      if (route.endsWith('/events/event/sources') && this.heldSources) {
        this.sourcesSignal = signal;
        return envelope(await this.heldSources.promise);
      }
      if (route.endsWith('/allowed')) return envelope({ items: [], next_cursor: null });
      if (route.endsWith('/search/status'))
        return envelope({ active: false, coverage: 'paused', marks: [], series: [] });
      if (route.endsWith('/sweeps'))
        return envelope([sweep(this.restored ? 'fresh-live' : 'live')]);
      if (route.endsWith('/observations')) return envelope([]);
      if (route.endsWith('/comparison')) {
        this.comparisonSignal = signal;
        return envelope(await this.comparison.promise);
      }
      if (route.endsWith('/history')) {
        this.historySignal = signal;
        if (this.heldHistory) return envelope(await this.heldHistory.promise);
        return envelope({
          items: this.restored ? [sweep('fresh')] : [sweep('A'), sweep('B')],
          next_cursor: null,
        });
      }
      if (route.includes('/history/')) {
        this.originalSignal = signal;
        if (this.heldOriginal) return envelope(await this.heldOriginal.promise);
        return envelope(sweep(decodeURIComponent(route.split('/').at(-1)!)));
      }
      throw Error('Unexpected fixture route: ' + path);
    },
  };
  rollback() {
    this.highWater = 10;
    this.restored = true;
    // Actual transport disconnect notification; no event sequence is relabelled.
    this.state?.(false, 'Owned connection interrupted');
  }
}

let root: Root | undefined;
let container: HTMLDivElement;
async function settle() {
  for (let i = 0; i < 10; i++)
    await act(async () => {
      await Promise.resolve();
    });
}
async function click(element: HTMLElement) {
  await act(async () => element.click());
  await settle();
}
function button(text: string, within: ParentNode = container) {
  const result = [...within.querySelectorAll<HTMLButtonElement>('button')].find(
    (b) => b.textContent?.trim() === text,
  );
  if (!result) throw Error('Missing button: ' + text);
  return result;
}
function row(id: string) {
  const result = [...container.querySelectorAll<HTMLElement>('[data-testid="history-row"]')].find(
    (e) => e.querySelector('small')?.textContent?.endsWith(id),
  );
  if (!result) throw Error('Missing history row: ' + id);
  return result;
}
async function mount(fixture: CoreFixture) {
  root = createRoot(container);
  await act(async () => root!.render(<App transport={fixture.transport} />));
  await settle();
}
async function load(fixture: CoreFixture) {
  await mount(fixture);
  await click(button('Research'));
  await click(button('Load history'));
  expect(row('A')).toBeTruthy();
}
async function rollback(fixture: CoreFixture) {
  await act(async () => {
    fixture.rollback();
    await vi.advanceTimersByTimeAsync(300);
  });
  await settle();
}
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal(
    'Worker',
    class {
      postMessage() {
        throw Error('No event delivery is needed in this component fixture');
      }
      terminate() {}
    },
  );
  let serial = 0;
  const cancelled = new Set<number>();
  vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => {
    const id = ++serial;
    queueMicrotask(() => {
      if (!cancelled.has(id)) fn(0);
    });
    return id;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => cancelled.add(id));
  localStorage.clear();
  setLocale('en');
  container = document.createElement('div');
  document.body.append(container);
});
afterEach(async () => {
  if (root) await act(async () => root!.unmount());
  root = undefined;
  container.remove();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it('does not attach late comparison A to the newly selected source B', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await click(row('B').querySelector<HTMLElement>('.history-select')!);
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  expect(row('B').classList.contains('selected')).toBe(true);
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
  expect(fixture.comparisonSignal?.aborted).toBe(true);
});
it('keeps the positive comparison when the original selected source is unchanged', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  expect(row('A').classList.contains('selected')).toBe(true);
  expect(container.textContent).toContain('Baseline comparison · compatible');
});
it('preserves an explicit incompatible-baseline response for its unchanged source', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await act(async () =>
    fixture.comparison.resolve({
      ...comparison('A'),
      compatible: false,
      reason: 'Original profile mismatch',
      items: [],
    }),
  );
  await settle();
  expect(row('A').classList.contains('selected')).toBe(true);
  expect(container.textContent).toContain('Original profile mismatch');
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
});
it('invalidates the original project comparison when its real hook changes selection', async () => {
  const fixture = new CoreFixture();
  root = createRoot(container);
  await act(async () => root!.render(<ProjectHost transport={fixture.transport} />));
  await settle();
  await click(button('Load history'));
  await click(button('Compare baseline', row('A')));
  await click(button('Select other project'));
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  expect(container.querySelectorAll('[data-testid="history-row"]')).toHaveLength(0);
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
  expect(fixture.comparisonSignal?.aborted).toBe(true);
});
it('aborts and invalidates a comparison when its history filter changes', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  const input = container.querySelector<HTMLInputElement>(
    'section[aria-label="Research workspace"] input[inputmode="numeric"]',
  )!;
  expect(input.closest('label')!.textContent).toContain('Minimum frequency');
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '100');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
  expect(fixture.comparisonSignal?.aborted).toBe(true);
});
it('aborts the underlying comparison request when the component unmounts', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await act(async () => root!.unmount());
  root = undefined;
  expect(fixture.comparisonSignal?.aborted).toBe(true);
  fixture.comparison.resolve(comparison('A'));
  await settle();
});
it('clears same-project history/comparison on actual HWM rollback and accepts a fresh selection', async () => {
  const fixture = new CoreFixture();
  await load(fixture);
  await click(button('Compare baseline', row('A')));
  await act(async () => fixture.comparison.resolve(comparison('A')));
  await settle();
  await rollback(fixture);
  expect(container.querySelectorAll('[data-testid="history-row"]')).toHaveLength(0);
  expect(container.textContent).not.toContain('Baseline comparison · compatible');
  await click(button('Load history'));
  expect(row('fresh').classList.contains('selected')).toBe(true);
  expect(fixture.requests.every((request) => request.body === undefined)).toBe(true);
});
it('aborts same-project pending history on HWM rollback and rejects its late old rows', async () => {
  const fixture = new CoreFixture();
  fixture.heldHistory = deferred();
  await mount(fixture);
  await click(button('Research'));
  await click(button('Load history'));
  await rollback(fixture);
  await act(async () =>
    fixture.heldHistory!.resolve({ items: [sweep('old-archive')], next_cursor: null }),
  );
  await settle();
  expect(container.querySelectorAll('[data-testid="history-row"]')).toHaveLength(0);
  expect(fixture.historySignal?.aborted).toBe(true);
});
it('clears event/original dialogs and rejects a late old original after same-project HWM rollback', async () => {
  const fixture = new CoreFixture();
  fixture.heldOriginal = deferred();
  await mount(fixture);
  await click(button('Details'));
  await click(button('Original sweep', container.querySelector('dialog')!));
  await rollback(fixture);
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
  await act(async () => fixture.heldOriginal!.resolve(sweep('old-original')));
  await settle();
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
  expect(fixture.originalSignal?.aborted).toBe(true);
});
it('does not start a new original lookup from a late pre-rollback event-source reply', async () => {
  const fixture = new CoreFixture();
  fixture.heldSources = deferred();
  await mount(fixture);
  await click(button('Details'));
  await click(button('Original sweep', container.querySelector('dialog')!));
  await rollback(fixture);
  await act(async () =>
    fixture.heldSources!.resolve({ items: [{ sweep_id: 'A' }], next_cursor: null }),
  );
  await settle();
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
  expect(fixture.requests.some((request) => request.path.endsWith('/history/A'))).toBe(false);
  expect(fixture.sourcesSignal?.aborted).toBe(true);
});
it('does not reopen an old event dialog when its pre-rollback metadata review acknowledges late', async () => {
  const fixture = new CoreFixture();
  fixture.heldReview = deferred();
  await mount(fixture);
  await click(button('Details'));
  const oldEvent = fixture.event();
  await click(button('Save review', container.querySelector('dialog')!));
  await rollback(fixture);
  await act(async () => fixture.heldReview!.resolve(oldEvent));
  await settle();
  expect(container.querySelectorAll('dialog')).toHaveLength(0);
  expect(fixture.reviewSignal?.aborted).toBe(true);
});
