// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SpectrumRenderer } from '../src/spectrum-renderer';
import { decimate } from '../src/pipeline';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { Spectrum } from '../src/spectrum';
import { t } from '../src/i18n';

let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;
const graphs: SpectrumRenderer[] = [];
beforeEach(() => {
  frames = new Map();
  nextFrame = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frames.set(++nextFrame, callback);
    return nextFrame;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
});
afterEach(() => {
  for (const graph of graphs.splice(0)) graph.dispose();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function tick() {
  const callbacks = [...frames];
  for (const [id, callback] of callbacks) {
    if (frames.delete(id)) callback(performance.now());
  }
  await Promise.resolve();
}
const range = { low: 100_000_000, high: 103_000_000 };
function sweep(index: number, unit = 'dBm') {
  return decimate({
    id: 'source-' + index,
    session_id: 'session',
    stream_id: 'analyzer',
    interval: { started_at: '2026-11-11T00:00:00Z', ended_at: '2026-11-11T00:00:01Z' },
    frequencies_hz: [range.low, range.low + 1_000_000, range.low + 2_000_000, range.high],
    levels: [-100, -20 - index, -90, -100],
    level_unit: unit,
    settings: {},
  });
}
function fixture() {
  const canvas = document.createElement('canvas'),
    fallback = document.createElement('canvas');
  vi.spyOn(canvas, 'getContext').mockReturnValue(null);
  vi.spyOn(fallback, 'getContext').mockReturnValue({
    setTransform() {},
    clearRect() {},
    beginPath() {},
    moveTo() {},
    lineTo() {},
    arc() {},
    fill() {},
    stroke() {},
  } as unknown as CanvasRenderingContext2D);
  const graph = new SpectrumRenderer(canvas, fallback, () => {});
  graphs.push(graph);
  graph.resize(800, 400);
  return { graph, canvas: fallback };
}

it('reserves a source even when another source arrives before its first draw', async () => {
  const { graph, canvas } = fixture();
  graph.update(sweep(0), undefined, range);
  graph.update(sweep(1), undefined, range);
  await tick();
  await tick();
  await tick();
  expect(canvas.dataset.sweepId).toBe('source-0');
  for (let i = 0; i < 4; i++) await tick();
  expect(canvas.dataset.sweepId).toBe('source-1');
});

it('retains FIFO sources with a bounded pending tail and coalesces only undrawn overflow', async () => {
  const { graph, canvas } = fixture();
  graph.update(sweep(0), undefined, range);
  await tick();
  for (let i = 1; i <= 66; i++) graph.update(sweep(i), undefined, range);
  expect(graph.presentationStatus).toEqual({ pendingSources: 64, coalescedSources: 2 });
  const observed: string[] = [];
  for (let i = 0; i < 280; i++) {
    await tick();
    const id = canvas.dataset.sweepId;
    if (id && id !== observed.at(-1)) observed.push(id);
  }
  expect(observed).toEqual([...Array.from({ length: 64 }, (_, i) => 'source-' + i), 'source-66']);
  expect(graph.presentationStatus).toEqual({ pendingSources: 0, coalescedSources: 2 });
});

it.each(['lost', 'restore'] as const)(
  'cancels queued work and obsolete markers on %s or disposal',
  async (event) => {
    const { graph, canvas } = fixture();
    graph.update(sweep(0), undefined, range);
    await tick();
    graph.update(sweep(1), undefined, range);
    graph[event]();
    const observed: string[] = [];
    for (let i = 0; i < 8; i++) {
      await tick();
      if (canvas.dataset.sweepId) observed.push(canvas.dataset.sweepId);
    }
    expect(new Set(observed)).toEqual(new Set(['source-1']));
    graph.update(sweep(2), undefined, range);
    await tick();
    graph.dispose();
    for (let i = 0; i < 4; i++) await tick();
    expect(canvas.dataset.sweepId).not.toBe('source-2');
    expect(graph.presentationStatus.pendingSources).toBe(0);
  },
);

it('preserves ownership when peak-dependent decimation selects different interior frequencies', async () => {
  const { graph, canvas } = fixture();
  const source = {
    id: 'decimated-A',
    session_id: 'session',
    stream_id: 'analyzer',
    interval: { started_at: '2026-11-11T00:00:00Z', ended_at: '2026-11-11T00:00:01Z' },
    frequencies_hz: Array.from({ length: 4096 }, (_, i) => 100_000_000 + i * 1000),
    levels: Array.from({ length: 4096 }, () => -100),
    level_unit: 'dBm',
    settings: {},
  };
  source.levels[17] = -20;
  const A = decimate(source);
  source.levels = Array.from({ length: 4096 }, () => -100);
  source.levels[3013] = -30;
  const B = decimate({ ...source, id: 'decimated-B' });
  expect(A.frequencies_hz).not.toEqual(B.frequencies_hz);
  const full = { low: 100_000_000, high: 104_095_000 };
  graph.update(A, undefined, full);
  await tick();
  graph.update(B, undefined, full);
  await tick();
  await tick();
  expect(canvas.dataset.sweepId).toBe(A.id);
  for (let i = 0; i < 4; i++) await tick();
  expect(canvas.dataset.sweepId).toBe(B.id);
});

it('keeps same-source view redraws independent of the two-frame source barrier', async () => {
  const { graph, canvas } = fixture();
  const source = sweep(0);
  graph.update(source, undefined, range);
  const acknowledged: string[] = [];
  for (let i = 0; i < 60; i++) {
    graph.update(source, undefined, { low: range.low + i, high: range.high - i });
    await tick();
    if (canvas.dataset.renderFrame !== acknowledged.at(-1) && canvas.dataset.renderFrame)
      acknowledged.push(canvas.dataset.renderFrame);
  }
  expect(acknowledged.length).toBeGreaterThanOrEqual(57);
  expect(canvas.dataset.sweepId).toBe(source.id);
});

it('applies a view change to the displayed owner and every queued source on the next draw', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  const context = {
    setTransform() {},
    clearRect() {},
    beginPath() {},
    moveTo() {},
    lineTo() {},
    arc() {},
    fill() {},
    stroke() {},
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(((kind: string) =>
    kind === '2d' ? context : null) as unknown as HTMLCanvasElement['getContext']);
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(private callback: ResizeObserverCallback) {}
      observe() {
        this.callback(
          [{ contentRect: { width: 900, height: 300 } }] as ResizeObserverEntry[],
          this as unknown as ResizeObserver,
        );
      }
      disconnect() {}
    },
  );
  const element = document.createElement('div');
  document.body.append(element);
  const root = createRoot(element);
  const A = sweep(0),
    B = sweep(1),
    C = sweep(2);
  const read = () => {
    const chart = element.querySelector('[data-testid="spectrum-chart"]') as HTMLElement;
    return {
      low: Number(chart.dataset.viewLow),
      high: Number(chart.dataset.viewHigh),
      label: element.querySelector('canvas.spectrum')?.getAttribute('aria-label'),
    };
  };
  const label = (source: typeof A) =>
    t('Spectrum: peak {level} {unit} at {frequency} Hz', {
      level: source.peak.level,
      unit: source.level_unit,
      frequency: source.peak.frequency_hz,
    });
  try {
    await act(() => root.render(createElement(Spectrum, { sweep: A })));
    await act(tick);
    await act(() => root.render(createElement(Spectrum, { sweep: B })));
    await act(() => root.render(createElement(Spectrum, { sweep: C })));
    await act(() => {
      [...element.querySelectorAll('button')]
        .find((button) => button.getAttribute('aria-label') === t('Zoom spectrum in'))!
        .click();
    });
    await act(tick);
    const afterPan = read();
    const following: ReturnType<typeof read>[] = [];
    for (let i = 0; i < 10; i++) {
      await act(tick);
      const current = read();
      if (current.label !== following.at(-1)?.label) following.push(current);
    }
    const zoomed = { low: 100_750_000, high: 102_250_000 };
    expect(afterPan).toEqual({ ...zoomed, label: label(A) });
    expect(following.map((row) => row.label)).toEqual([label(A), label(B), label(C)]);
    expect(following.every((row) => row.low === zoomed.low && row.high === zoomed.high)).toBe(true);
  } finally {
    await act(() => root.unmount());
    element.remove();
  }
});
