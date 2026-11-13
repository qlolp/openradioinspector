// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { LiveSourceCursor, type LiveSources } from '../src/live-presentation';
import { decimate } from '../src/pipeline';
import { Spectrum } from '../src/spectrum';
import { t } from '../src/i18n';

const range = { low: 100_000_000, high: 103_000_000 };
function source(id: string, peak = -20) {
  return decimate({
    id,
    session_id: 'session-e\u0301',
    stream_id: 'analyzer',
    interval: {
      started_at: '2026-11-11T00:00:00.123456Z',
      ended_at: '2026-11-11T00:00:01.123456Z',
    },
    frequencies_hz: [range.low, range.low + 1_000_000, range.low + 2_000_000, range.high],
    levels: [-100, peak, -90, -100],
    level_unit: 'raw S-meter',
    settings: {},
  });
}
const feed = (epoch: object, total: bigint, rows: LiveSources['rows']): LiveSources => ({
  rows,
  presentation: { epoch, total },
});

it('offers the first coalesced committed suffix by reference without treating seed history as new', () => {
  const cursor = new LiveSourceCursor();
  const epoch = {};
  const seed = source('seed');
  expect(cursor.take(feed(epoch, 0n, [seed]))).toEqual({ reset: true, rows: [] });
  const A = source('A'),
    B = source('B'),
    C = source('C');
  const batch = cursor.take(feed(epoch, 3n, [seed, A, B, C]));
  expect(batch.reset).toBe(false);
  expect(batch.rows).toEqual([A, B, C]);
  expect(batch.rows[0]).toBe(A);
  expect(batch.rows[0].levels).toBe(A.levels);
  expect(batch.rows[0].frequencies_hz).toBe(A.frequencies_hz);
  expect(cursor.take(feed(epoch, 3n, [seed, A, B, C])).rows).toEqual([]);
  expect(cursor.presentationStatus).toEqual({ retainedRowEvictions: 0n });
});

it('does not depend on a prior publication or safe-integer conversion of the append count', () => {
  const cursor = new LiveSourceCursor();
  const epoch = {};
  const A = source('A'),
    B = source('B');
  const total = 9007199254740993n;
  expect(cursor.take(feed(epoch, total, [A])).rows).toEqual([A]);
  expect(cursor.presentationStatus.retainedRowEvictions).toBe(total - 1n);
  expect(cursor.take(feed(epoch, total + 1n, [A, B])).rows).toEqual([B]);
  expect(cursor.presentationStatus.retainedRowEvictions).toBe(total - 1n);
});

it('reports evicted unseen rows exactly and never replays the retained history afterward', () => {
  const cursor = new LiveSourceCursor();
  const epoch = {};
  const rows = Array.from({ length: 256 }, (_, index) => source(String(index + 44)));
  expect(cursor.take(feed(epoch, 300n, rows)).rows).toEqual(rows);
  expect(cursor.presentationStatus.retainedRowEvictions).toBe(44n);
  const latest = source('300');
  expect(cursor.take(feed(epoch, 301n, [...rows.slice(1), latest])).rows).toEqual([latest]);
  expect(cursor.take(feed(epoch, 301n, [...rows.slice(1), latest])).rows).toEqual([]);
  expect(cursor.presentationStatus.retainedRowEvictions).toBe(44n);
});

it('resets a same-ID baseline by epoch and refuses an unfenced counter rollback', () => {
  const cursor = new LiveSourceCursor();
  const oldEpoch = {},
    newEpoch = {};
  const old = source('same-id', -20),
    replacement = source('same-id', -60);
  cursor.take(feed(oldEpoch, 1n, [old]));
  expect(() => cursor.take(feed(oldEpoch, 0n, [replacement]))).toThrow(/backward/);
  expect(cursor.take(feed(oldEpoch, 1n, [old])).rows).toEqual([]);
  expect(cursor.take(feed(newEpoch, 0n, [old]))).toEqual({ reset: true, rows: [] });
  const batch = cursor.take(feed(newEpoch, 1n, [old, replacement]));
  expect(batch.rows).toEqual([replacement]);
  expect(batch.rows[0]).toBe(replacement);
  expect(cursor.presentationStatus.retainedRowEvictions).toBe(0n);
});

let frames: Map<number, FrameRequestCallback>;
let serial: number;
beforeEach(() => {
  frames = new Map();
  serial = 0;
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frames.set(++serial, callback);
    return serial;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function tick() {
  for (const [id, callback] of [...frames]) if (frames.delete(id)) callback(performance.now());
  await Promise.resolve();
}
function component() {
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
  const canvas = () => element.querySelector('canvas.spectrum') as HTMLCanvasElement;
  return {
    root,
    element,
    canvas,
    close: async () => {
      await act(() => root.unmount());
      element.remove();
    },
  };
}

it.each([
  { name: 'tied', peaks: [-20, -20, -20] },
  { name: 'rising', peaks: [-30, -20, -10] },
])(
  'admits A/B/C in order when a small coalesced snapshot fits ($name peaks)',
  async ({ peaks }) => {
    const fixture = component();
    const rows = [source('A', peaks[0]), source('B', peaks[1]), source('C', peaks[2])];
    const liveSources = feed({}, 3n, rows);
    try {
      await act(() =>
        fixture.root.render(createElement(Spectrum, { sweep: rows[2], liveSources })),
      );
      const observed: string[] = [];
      for (let index = 0; index < 14; index++) {
        await act(tick);
        const id = fixture.canvas().dataset.sweepId;
        if (id && id !== observed.at(-1)) observed.push(id);
      }
      expect(observed).toEqual(['A', 'B', 'C']);
    } finally {
      await fixture.close();
    }
  },
);

it('shows the latest burst source and explicitly reports display coalescing, retaining the first maximum', async () => {
  const fixture = component();
  const rows = Array.from({ length: 70 }, (_, i) =>
    source('burst-' + i, i === 22 || i === 23 ? -1 : -50),
  );
  const original = rows.map((row) => [...row.levels]);
  try {
    await act(() =>
      fixture.root.render(
        createElement(Spectrum, {
          sweep: rows.at(-1),
          liveSources: feed({}, 70n, rows),
        }),
      ),
    );
    const observed: string[] = [];
    for (let index = 0; index < 11; index++) {
      await act(tick);
      const id = fixture.canvas().dataset.sweepId;
      if (id && id !== observed.at(-1)) observed.push(id);
    }
    expect(observed).toEqual(['burst-0', 'burst-22', 'burst-69']);
    expect(
      fixture.element.querySelector('[data-testid="spectrum-coalesced"]')?.textContent,
    ).toContain(
      t('Combined {count} display updates. Original sweeps remain archived.', { count: 67 }),
    );
    expect(rows.map((row) => [...row.levels])).toEqual(original);
  } finally {
    await fixture.close();
  }
});

it('cancels an old marker and labels across a same-ID epoch reset before a replacement draws', async () => {
  const fixture = component();
  const old = source('same-id', -20),
    replacement = source('same-id', -60);
  const epoch = {};
  try {
    await act(() =>
      fixture.root.render(
        createElement(Spectrum, { sweep: old, liveSources: feed({}, 1n, [old]) }),
      ),
    );
    await act(tick); // Old pixels submitted; its two-RAF marker is still pending.
    await act(() =>
      fixture.root.render(createElement(Spectrum, { liveSources: feed(epoch, 0n, []) })),
    );
    expect(fixture.canvas().dataset.sweepId).toBeUndefined();
    for (let index = 0; index < 3; index++) await act(tick);
    expect(fixture.canvas().dataset.sweepId).toBeUndefined();
    await act(() =>
      fixture.root.render(
        createElement(Spectrum, {
          sweep: replacement,
          liveSources: feed(epoch, 1n, [replacement]),
        }),
      ),
    );
    for (let index = 0; index < 4; index++) await act(tick);
    expect(fixture.canvas().dataset.sweepId).toBe('same-id');
    expect(fixture.canvas().getAttribute('aria-label')).toBe(
      t('Spectrum: peak {level} {unit} at {frequency} Hz', {
        level: -60,
        unit: 'raw S-meter',
        frequency: 101_000_000,
      }),
    );
  } finally {
    await fixture.close();
  }
});
