// Diagnostic only: real App/hooks/store/charts and native browser presentation.
// Only the Worker completion scheduling boundary and read-only Core replies are controlled.
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
import { App } from '../src/App';
import { parseFrame, type FrameInput } from '../src/pipeline';
import type { Connection } from '../src/transport';
import type { EventSequence } from '../src/sequence';
import type { Sweep } from '../src/types';
import '../src/styles.css';
import { captureSeedOrder } from './live-seed-order.instrument';

const initial = 9007199254740992n;
const project = {
  id: 'owned-publication-project',
  name: 'Owned publication diagnostic',
  site: 'Synthetic; no instruments',
  state: 'draft',
  active_session_id: 'session-e\u0301',
  analyzer_stream_id: 'analyzer',
  receiver_stream_id: 'receiver',
};
const envelope = (data: unknown) => ({
  contract: 'openradioinspector-platform',
  contract_version: 1,
  data,
});

export async function runLivePublicationDiagnostic(mode: 'coalesced' | 'spaced' | 'seed-replay') {
  const capture = mode === 'seed-replay' ? captureSeedOrder() : undefined;
  const element = document.createElement('div');
  document.body.append(element);
  localStorage.clear();
  localStorage.setItem('ori-modern-locale', 'en');
  localStorage.setItem('ori-modern-project', project.id);
  localStorage.setItem('ori-modern-workspace', 'monitoring');
  const originalWorker = window.Worker;
  let frame = 0;
  let frameHandle = 0;
  const clock = () => {
    frame++;
    frameHandle = requestAnimationFrame(clock);
  };
  frameHandle = requestAnimationFrame(clock);
  const tick = () => new Promise<number>(requestAnimationFrame);
  const parsed: {
    id: string;
    sequence: string;
    frame: number;
    sweep: NonNullable<ReturnType<typeof parseFrame>['sweep']>;
  }[] = [];
  const applied: { id: string; cursor: string; frame: number }[] = [];
  const workers: ControlledParser[] = [];
  let pending = 0;
  let maximumPending = 0;
  let parserReleased = mode !== 'seed-replay';
  const held: (() => void)[] = [];
  class ControlledParser {
    onmessage?: (event: { data: unknown }) => void;
    terminated = false;
    constructor() {
      workers.push(this);
    }
    postMessage(message: { raw: FrameInput; generation: number }) {
      maximumPending = Math.max(maximumPending, ++pending);
      const complete = () => {
        pending--;
        if (this.terminated) return;
        try {
          const value = parseFrame(message.raw);
          if (value.sweep)
            parsed.push({
              id: value.sweep.id,
              sequence: String(value.sequence),
              frame,
              sweep: value.sweep,
            });
          this.onmessage?.({ data: { frame: value, generation: message.generation } });
        } catch (error) {
          this.onmessage?.({ data: { error: String(error), generation: message.generation } });
        }
      };
      if (parserReleased) queueMicrotask(complete);
      else held.push(complete);
    }
    terminate() {
      this.terminated = true;
    }
  }
  window.Worker = ControlledParser as unknown as typeof Worker;
  let receive: Parameters<Connection['subscribe']>[1] | undefined;
  let last = initial;
  let seedRequests = 0;
  let subscriptionClosed = false;
  const requests: string[] = [];
  const transport: Connection = {
    native: false,
    async connect() {
      throw Error('No connection mutation is allowed');
    },
    async pair() {
      throw Error('No pairing is allowed');
    },
    async revokePairing() {
      throw Error('No revocation is allowed');
    },
    async audio() {
      throw Error('No audio is allowed');
    },
    async openLegacy() {
      throw Error('No external navigation is allowed');
    },
    async subscribe(_after, callback, state) {
      receive = callback;
      state(true);
      return () => {
        subscriptionClosed = true;
      };
    },
    async request(path, body) {
      if (body !== undefined) throw Error('No API mutation is allowed');
      requests.push(path);
      const route = new URL(path, 'http://owned.invalid').pathname.replace('/api/v1', '');
      if (route === '/health')
        return envelope({ status: 'ok', last_event_id: last, sessions: 1, running_sessions: 0 });
      if (route === '/capabilities')
        return envelope({ features: {}, limitations: [], hardware_validated: false });
      if (route === '/sessions')
        return envelope([
          { id: project.active_session_id, name: 'Synthetic', state: 'draft', streams: [] },
        ]);
      if (route === '/monitoring-projects')
        return envelope({ items: [project], next_cursor: null });
      if (route.endsWith('/search/status'))
        return envelope({ active: false, coverage: 'paused', marks: [], series: [] });
      if (route.endsWith('/status'))
        return envelope({
          project,
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
      if (route.endsWith('/events') || route.endsWith('/allowed'))
        return envelope({ items: [], next_cursor: null });
      if (route.endsWith('/sweeps')) {
        seedRequests++;
        return envelope(mode === 'seed-replay' ? [sources[1]] : []);
      }
      if (route.endsWith('/observations')) return envelope([]);
      throw Error('Unexpected read-only fixture route: ' + path);
    },
  };
  const root = createRoot(element);
  const chart = () =>
    element.querySelector('.live-grid [data-testid="spectrum-chart"]') as HTMLElement | null;
  const read = () => {
    const canvas = chart()?.querySelector(
      'canvas.spectrum:not([hidden])',
    ) as HTMLCanvasElement | null;
    const context = canvas?.getContext('2d');
    let pixels: number | null = null;
    if (context && canvas!.width > 1 && canvas!.height > 1) {
      const bytes = context.getImageData(0, 0, canvas!.width, canvas!.height).data;
      let hash = 2166136261;
      for (let index = 0; index < bytes.length; index += 4)
        hash = Math.imul(
          hash ^ bytes[index] ^ (bytes[index + 1] << 8) ^ (bytes[index + 3] << 16),
          16777619,
        );
      pixels = hash >>> 0;
    }
    return {
      frame,
      marker: canvas?.dataset.sweepId ?? null,
      pixels,
      width: canvas?.width ?? 0,
      height: canvas?.height ?? 0,
      label: canvas?.getAttribute('aria-label') ?? null,
      unit: chart()?.querySelector('.spectrum-unit')?.textContent ?? null,
      sourceFooter:
        element.querySelector('.live-grid [data-testid="source-id"]')?.textContent ?? null,
      retainedRowsLabel:
        element.querySelector('.live-grid .waterfall[role="img"]')?.getAttribute('aria-label') ??
        null,
      action: element.querySelector('[data-testid="action-state"]')?.textContent ?? null,
    };
  };
  const observed: (ReturnType<typeof read> & { id: string })[] = [];
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      const canvas = record.target as HTMLCanvasElement;
      const id = canvas.dataset.sweepId;
      if (id && id.startsWith('committed-e\u0301-') && !observed.some((row) => row.id === id))
        observed.push({ ...read(), id });
      if (id) capture?.record({ marker: id, nativeFrame: frame });
    }
  });
  observer.observe(element, {
    subtree: true,
    attributes: true,
    attributeFilter: ['data-sweep-id'],
  });
  const sources: Sweep[] = ['A', 'B', 'C'].map((letter, index) => {
    const levels = Array.from(
      { length: 290 },
      (_, point) => -110 + ((point * 7 + index * 17) % 55),
    );
    levels[17] = -0;
    return {
      id: 'committed-e\u0301-' + letter,
      session_id: project.active_session_id,
      stream_id: project.analyzer_stream_id,
      interval: {
        started_at: `2026-11-12T00:00:0${index}.123456Z`,
        ended_at: `2026-11-12T00:00:0${index}.223456Z`,
      },
      frequencies_hz: Array.from({ length: 290 }, (_, point) => 100_000_000 + point * 10_000),
      levels,
      level_unit: 'raw S-meter',
      settings: { synthetic: true, original: 'unchanged-e\u0301' },
    };
  });
  const originals = sources.map((source) => JSON.stringify(source));
  let retainedForSeedOracle = false;
  let seedDraw: ReturnType<typeof read> | undefined;
  const seedObserver = new MutationObserver(() => {
    if (!capture || seedDraw) return;
    const value = read();
    if (
      value.sourceFooter === sources[1].id &&
      value.unit === sources[1].level_unit &&
      value.pixels !== null &&
      value.marker === null
    ) {
      seedDraw = value;
      capture.record({ actualSeedDraw: value });
    }
  });
  seedObserver.observe(element, {
    subtree: true,
    attributes: true,
    attributeFilter: ['aria-label', 'width', 'height'],
  });
  const close = () => {
    observer.disconnect();
    seedObserver.disconnect();
    flushSync(() => root.unmount());
    element.remove();
    cancelAnimationFrame(frameHandle);
    window.Worker = originalWorker;
    capture?.close();
    delete (window as any).__closeLiveSeedOrderForTest;
    const receipt = {
      subscriptionClosed,
      pending,
      allParsersTerminated: workers.every((worker) => worker.terminated),
    };
    if (!receipt.allParsersTerminated || pending || !subscriptionClosed)
      throw Error('Original subscription/parser cleanup was incomplete');
    return receipt;
  };
  async function deliver(source: Sweep, index: number) {
    if (!receive) throw Error('Original subscription is not ready');
    const sequence = initial + BigInt(index + 1);
    const rawSweep = JSON.stringify(source).replace(/("levels":\[[^\]]*)/, (part) => {
      const values = part.slice(part.indexOf('[') + 1).split(',');
      values[17] = '-0.0';
      return '"levels":[' + values.join(',');
    });
    const raw =
      '{"contract":"openradioinspector-platform","contract_version":1,"data":{"sequence":' +
      sequence +
      ',"event_type":"sweep.received","payload":{"sweep":' +
      rawSweep +
      '}}}';
    const cursor = await receive(raw);
    if (String(cursor) !== String(sequence)) throw Error('Applied cursor did not match source');
    last = sequence;
    applied.push({ id: source.id, cursor: String(cursor), frame });
  }
  try {
    flushSync(() => root.render(createElement(App, { transport })));
    let ready = false;
    for (let index = 0; index < 40; index++) {
      await tick();
      const current = read();
      if (
        receive &&
        seedRequests &&
        current.pixels !== null &&
        !current.marker &&
        (mode !== 'seed-replay' || seedDraw !== undefined) &&
        current.width > 1 &&
        current.height > 1
      ) {
        ready = true;
        break;
      }
    }
    const blank = read();
    if (!ready)
      throw Error(
        'Blank native canvas/empty seed did not settle: ' + JSON.stringify({ blank, requests }),
      );
    let heldParserItems = 0;
    if (mode === 'seed-replay') {
      const first = deliver(sources[0], 0);
      heldParserItems = held.length;
      capture!.record({ beforeParserRelease: { seedDraw, heldParserItems, snapshot: read() } });
      parserReleased = true;
      for (const complete of held.splice(0)) queueMicrotask(complete);
      await first;
      await deliver(sources[1], 1);
    } else if (mode === 'coalesced') {
      // One native RAF callback plus its microtask checkpoint. No RAF/MO/Canvas
      // replacement: all three controlled parser completions precede publication.
      await new Promise<void>((resolve, reject) =>
        requestAnimationFrame(() => {
          void (async () => {
            for (let index = 0; index < sources.length; index++)
              await deliver(sources[index], index);
          })().then(resolve, reject);
        }),
      );
    } else {
      for (let index = 0; index < sources.length; index++) {
        await deliver(sources[index], index);
        for (
          let count = 0;
          count < 20 && !observed.some((row) => row.id === sources[index].id);
          count++
        )
          await tick();
        if (!observed.some((row) => row.id === sources[index].id))
          throw Error('Spaced source never reached its native marker');
      }
    }
    const afterApplied = read();
    for (let index = 0; index < 20; index++) await tick();
    const final = read();
    const parsedNegativeZero = parsed.every(({ sweep }) => Object.is(sweep.levels[17], -0));
    const parsedArraysUnchanged = parsed.every(
      ({ sweep }, index) =>
        sweep.levels.length === 290 &&
        sweep.frequencies_hz.length === 290 &&
        sweep.frequencies_hz.every((hz, point) => hz === sources[index].frequencies_hz[point]) &&
        sweep.levels.every((level, point) => Object.is(level, sources[index].levels[point])) &&
        sweep.id === sources[index].id &&
        sweep.session_id === sources[index].session_id &&
        sweep.stream_id === sources[index].stream_id &&
        sweep.interval.started_at === sources[index].interval.started_at &&
        sweep.interval.ended_at === sources[index].interval.ended_at &&
        sweep.level_unit === sources[index].level_unit,
    );
    if (mode === 'seed-replay') {
      // Keep the real DOM alive only for this test's unchanged five-second
      // final-marker assertion. The test always invokes this close in finally.
      (window as any).__closeLiveSeedOrderForTest = () => {
        const final = read();
        const entries = capture!.entries;
        const dropped = capture!.dropped;
        return { final, capture: { entries, dropped }, cleanup: close() };
      };
      retainedForSeedOracle = true;
    }
    return {
      mode,
      scope:
        'controlled Worker completion scheduling; production parseFrame/App/hooks/LiveStore/Spectrum; native Canvas/RAF/MO; no Core/real-worker/latency-acceptance claim',
      initialCursor: String(initial),
      blank,
      afterApplied,
      final,
      applied,
      parsed: parsed.map(({ id, sequence, frame }) => ({ id, sequence, frame })),
      maximumPending,
      observed,
      parsedNegativeZero,
      parsedArraysUnchanged,
      inputArraysUnchanged: sources.every(
        (source, index) =>
          JSON.stringify(source) === originals[index] && Object.is(source.levels[17], -0),
      ),
      requests,
      seedDraw,
      heldParserItems,
      capture: capture ? { entries: capture.entries, dropped: capture.dropped } : undefined,
      get subscriptionClosed() {
        return subscriptionClosed;
      },
    };
  } finally {
    if (!retainedForSeedOracle) close();
  }
}
