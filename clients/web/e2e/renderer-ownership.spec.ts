import { expect, test } from '@playwright/test';
import { writeFile } from 'node:fs/promises';

test('real Canvas draws retain matching labels and observable source markers, while changed contexts cancel old markers', async ({
  page,
}, testInfo) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
  // A static test entry settles React dependencies before the evaluated case.
  await page.goto('./e2e/renderer-component.html', { waitUntil: 'networkidle' });
  await page.waitForFunction(
    () => typeof (window as any).mountSpectrumFixtureForBrowserTest === 'function',
  );
  const result = await page.evaluate(async () => {
    const tick = () => new Promise<number>(requestAnimationFrame);
    const source = (id: string, base: number, levels: number[], unit: string) => ({
      id,
      session_id: 'display-scope',
      stream_id: 'analyzer',
      interval: { started_at: '2026-11-11T00:00:00Z', ended_at: '2026-11-11T00:00:01Z' },
      frequencies_hz: [base, base + 1_000_000, base + 2_000_000, base + 3_000_000],
      levels,
      level_unit: unit,
      settings: { synthetic: true },
    });
    const hash = (canvas: HTMLCanvasElement) => {
      const bytes = canvas.getContext('2d')!.getImageData(0, 0, canvas.width, canvas.height).data;
      let value = 2166136261;
      for (let i = 0; i < bytes.length; i += 4)
        value = Math.imul(value ^ bytes[i] ^ (bytes[i + 1] << 8) ^ (bytes[i + 3] << 16), 16777619);
      return value >>> 0;
    };
    const scenario = async (kind: 'same' | 'grid' | 'unit' | 'scope' | 'pan') => {
      const element = document.createElement('div');
      element.style.cssText = 'width:900px;height:600px';
      document.body.append(element);
      // The same raw unit has source-dependent level bounds, unlike fixed dBm axes.
      const A = source(kind + '-A', 100_000_000, [-100, -20, -90, -100], 'raw S-meter');
      const B = source(
        kind + '-B',
        kind === 'grid' ? 200_000_000 : 100_000_000,
        [-220, -190, -60, -210],
        kind === 'unit' ? 'dBµV' : 'raw S-meter',
      );
      if (kind === 'scope') {
        B.session_id = 'new-session';
        B.stream_id = 'new-stream';
      }
      const fixture = (window as any).mountSpectrumFixtureForBrowserTest(element);
      const read = () => {
        const canvas = element.querySelector('canvas.spectrum:not([hidden])') as HTMLCanvasElement;
        const chart = element.querySelector('[data-testid="spectrum-chart"]') as HTMLElement;
        return {
          pixels: canvas && canvas.width > 1 ? hash(canvas) : null,
          width: canvas?.width,
          height: canvas?.height,
          label: canvas?.getAttribute('aria-label'),
          unit: element.querySelector('.spectrum-unit')?.textContent,
          low: chart?.dataset.viewLow,
          high: chart?.dataset.viewHigh,
          levels: element.querySelector('.spectrum-level-axis')?.textContent,
          marker: canvas?.dataset.sweepId ?? null,
        };
      };
      const observed: (ReturnType<typeof read> & { id: string })[] = [];
      const observer = new MutationObserver((records) => {
        for (const record of records) {
          const canvas = record.target as HTMLCanvasElement;
          const id = canvas.dataset.sweepId;
          if (id && (id === A.id || id === B.id) && !observed.some((row) => row.id === id))
            observed.push({ ...read(), id });
        }
      });
      observer.observe(element, {
        subtree: true,
        attributes: true,
        attributeFilter: ['data-sweep-id'],
      });
      let panObserver: MutationObserver | undefined;
      try {
        // Establish the real ResizeObserver geometry with no source offered.
        // This is functional setup, not a warmup of the performance workload.
        let beforeSource = read();
        let geometryReady = false;
        const geometrySamples = [beforeSource];
        for (let i = 0; i < 8; i++) {
          await tick();
          const current = read();
          geometrySamples.push(current);
          if (
            current.pixels !== null &&
            current.height! > 1 &&
            current.width === beforeSource.width &&
            current.height === beforeSource.height
          ) {
            beforeSource = current;
            geometryReady = true;
            break;
          }
          beforeSource = current;
        }
        if (!geometryReady || beforeSource.marker)
          throw Error(
            'Expected a visible blank canvas before offering the first source: ' +
              JSON.stringify({
                geometrySamples,
                mode: element.querySelector('[data-testid="spectrum-renderer-state"]')?.textContent,
                plot: element.querySelector('.spectrum-plot')?.getBoundingClientRect().toJSON(),
                canvases: [...element.querySelectorAll('canvas')].map((canvas) => ({
                  hidden: canvas.hidden,
                  className: canvas.className,
                  width: canvas.width,
                  height: canvas.height,
                  display: getComputedStyle(canvas).display,
                })),
              }),
          );
        let initial: ReturnType<typeof read> | undefined;
        let queued: ReturnType<typeof read> | undefined;
        let afterPan: ReturnType<typeof read> | undefined;
        if (kind === 'pan') {
          // Trigger the input from A's actual draw notification, before its first
          // marker RAF. Both observers and all animation frames remain native.
          panObserver = new MutationObserver(() => {
            const current = read();
            if (initial || current.unit !== A.level_unit || current.marker) return;
            initial = current;
            fixture.show(B);
            queued = read();
            (
              element.querySelector('button[aria-label="Zoom spectrum in"]') as HTMLButtonElement
            ).click();
            requestAnimationFrame(() => {
              afterPan = read();
            });
          });
          panObserver.observe(element, {
            subtree: true,
            attributes: true,
            attributeFilter: ['aria-label'],
          });
        }
        fixture.show(A);
        for (let i = 0; i < 6; i++) {
          await tick();
          if (kind === 'pan') {
            if (afterPan) break;
          } else {
            initial = read();
            if (initial.pixels !== null) break;
          }
        }
        if (!initial || initial.pixels === null || initial.marker)
          throw Error('Expected A actual pixels before its original two-RAF marker');
        if (kind !== 'pan') {
          fixture.show(B);
          queued = read();
        }
        if (!queued || (kind === 'pan' && !afterPan))
          throw Error('Expected queued B and the next native input draw');
        await tick();
        await tick();
        await Promise.resolve();
        const afterTwoRafs = { ...read(), observed: observed.map((row) => ({ ...row })) };
        for (let i = 0; i < 5; i++) await tick();
        return { beforeSource, initial, queued, afterPan, afterTwoRafs, final: read(), observed };
      } finally {
        panObserver?.disconnect();
        observer.disconnect();
        fixture.close();
        element.remove();
      }
    };
    return {
      same: await scenario('same'),
      grid: await scenario('grid'),
      unit: await scenario('unit'),
      scope: await scenario('scope'),
      pan: await scenario('pan'),
    };
  });
  const evidence = testInfo.outputPath('real-canvas-raf-and-mutation-observer.json');
  await writeFile(evidence, JSON.stringify(result, null, 2));
  await testInfo.attach('real-canvas-raf-and-mutation-observer.json', {
    path: evidence,
    contentType: 'application/json',
  });
  const same = result.same;
  expect(same.queued.pixels).toBe(same.initial.pixels);
  expect(same.queued.label).toBe(same.initial.label);
  expect(same.queued.unit).toBe(same.initial.unit);
  expect(same.queued.low).toBe(same.initial.low);
  expect(same.queued.high).toBe(same.initial.high);
  expect(same.queued.levels).toBe(same.initial.levels);
  expect(same.afterTwoRafs.observed.map((row) => row.id)).toEqual(['same-A']);
  expect(same.observed.map((row) => row.id)).toEqual(['same-A', 'same-B']);
  expect(same.observed[0].pixels).toBe(same.initial.pixels);
  expect(same.observed[0].label).toBe(same.initial.label);
  expect(same.observed[0].levels).toBe(same.initial.levels);
  expect(same.observed[0].unit).toBe(same.initial.unit);
  expect(same.observed[0].low).toBe(same.initial.low);
  expect(same.observed[0].high).toBe(same.initial.high);
  expect(same.observed[0].width).toBe(same.initial.width);
  expect(same.observed[0].height).toBe(same.initial.height);
  expect(same.observed[1].pixels).not.toBe(same.initial.pixels);
  expect(same.observed[1].label).toContain('-60');
  expect(same.final.marker).toBe('same-B');
  expect(same.final.label).toContain('-60');
  expect(same.final.levels).not.toBe(same.initial.levels);
  for (const kind of ['grid', 'unit', 'scope'] as const) {
    const changed = result[kind];
    expect(changed.queued.pixels, kind + ' must hide the obsolete displayed context').toBeNull();
    expect(changed.observed.map((row) => row.id)).toEqual([kind + '-B']);
    expect(changed.final.marker).toBe(kind + '-B');
  }
  const pan = result.pan;
  const zoomed = { low: '100750000', high: '102250000' };
  expect(pan.afterPan).toMatchObject({
    ...zoomed,
    marker: null,
    label: pan.initial.label,
    levels: pan.initial.levels,
  });
  expect(pan.afterPan!.pixels).not.toBe(pan.initial.pixels);
  expect(pan.observed.map((row) => row.id)).toEqual(['pan-A', 'pan-B']);
  expect(pan.observed[0]).toMatchObject({ ...zoomed, label: pan.initial.label });
  expect(pan.observed[0].pixels).toBe(pan.afterPan!.pixels);
  expect(pan.observed[1]).toMatchObject(zoomed);
  expect(pan.final).toMatchObject({ ...zoomed, marker: 'pan-B' });
});
