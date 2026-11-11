import { expect, test } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { openDesk } from './helpers';

test('measures acknowledged pan/zoom draws for a real archived 4096-point source', async ({
  page,
  request,
  browser,
}, testInfo) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
  const base = 'http://127.0.0.1:18771/api/v1';
  const template = (await (await request.get(base + '/sessions')).json()).data[0];
  const sessionResponse = await request.post(base + '/sessions', {
    data: {
      name: 'Owned interactive display probe',
      streams: template.streams.map(({ id, session_id, state, ...stream }: any) => stream),
    },
  });
  expect(sessionResponse.ok()).toBe(true);
  const session = (await sessionResponse.json()).data;
  const projectResponse = await request.post(base + '/monitoring-projects', {
    data: {
      name: 'Owned interactive display probe',
      site: 'Synthetic unarmed only',
      session_id: session.id,
    },
  });
  expect(projectResponse.ok()).toBe(true);
  const project = (await projectResponse.json()).data;
  const sourceId = 'interactive-4096-source';
  const levels = Array.from({ length: 4096 }, (_, i) => -100 + Math.sin(i * 0.08) * 3);
  levels[1937] = -35;
  levels[1980] = -35;
  const source = await request.post(base + '/sessions/' + session.id + '/sweeps', {
    data: {
      id: sourceId,
      stream_id: session.streams[0].id,
      interval: {
        started_at: '2026-11-11T12:00:00.123456Z',
        ended_at: '2026-11-11T12:00:00.223456Z',
      },
      frequencies_hz: Array.from({ length: 4096 }, (_, i) => 88_000_000 + i * 4000),
      levels,
      level_unit: 'dBm',
      settings: { synthetic: true },
    },
  });
  expect(source.ok()).toBe(true);
  await openDesk(page);
  await page.getByTestId('project-select').selectOption(project.id);
  await expect(page.getByTestId('guard-state')).toHaveText('Draft');
  const chart = page.getByTestId('spectrum-chart').first();
  await expect(chart.locator('canvas.spectrum')).toHaveAttribute('data-sweep-id', sourceId);
  await expect(page.getByTestId('spectrum-renderer-state').first()).toHaveText('WebGL2');
  await chart.getByRole('button', { name: 'Zoom spectrum in', exact: true }).click();
  await chart.getByRole('button', { name: 'Zoom spectrum in', exact: true }).click();
  // Explicit draw barriers finish setup. No timed warmup, shortened denominator
  // or timeout increase can turn an overloaded host into an acceptance pass.
  await page.evaluate(async () => {
    for (let i = 0; i < 4; i++) await new Promise(requestAnimationFrame);
  });
  const mutations: string[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'GET') mutations.push(request.url());
  });
  const cpuBefore = await readFile('/sys/fs/cgroup/cpu.stat', 'utf8').catch(() => null);
  const result = await chart.evaluate(async (root) => {
    const plot = root.querySelector('.spectrum-plot') as HTMLElement;
    const canvas = root.querySelector('canvas.spectrum') as HTMLCanvasElement;
    const rect = plot.getBoundingClientRect();
    const gl = canvas.getContext('webgl2')!;
    const debug = gl.getExtension('WEBGL_debug_renderer_info');
    const frames: { frame: number; ms: number }[] = [];
    const ranges: string[] = [];
    let offered = 0,
      previousRange = '',
      changed = 0;
    const observer = new MutationObserver(() => {
      frames.push({ frame: Number(canvas.dataset.renderFrame), ms: performance.now() });
    });
    observer.observe(canvas, { attributes: true, attributeFilter: ['data-render-frame'] });
    const started = performance.now(),
      duration = 5000;
    await new Promise<void>((resolve) => {
      const tick = () => {
        const now = performance.now(),
          range =
            (root as HTMLElement).dataset.viewLow + '/' + (root as HTMLElement).dataset.viewHigh;
        if (previousRange && range !== previousRange) changed++;
        previousRange = range;
        ranges.push(range);
        if (now - started >= duration) {
          resolve();
          return;
        }
        if (now - started < duration / 2)
          plot.dispatchEvent(
            new KeyboardEvent('keydown', {
              key: offered % 2 ? 'ArrowRight' : 'ArrowLeft',
              bubbles: true,
            }),
          );
        else
          plot.dispatchEvent(
            new WheelEvent('wheel', {
              deltaY: offered % 2 ? 8 : -8,
              clientX: rect.x + rect.width * 0.5,
              clientY: rect.y + rect.height * 0.5,
              bubbles: true,
              cancelable: true,
            }),
          );
        offered++;
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    const offeredElapsed = performance.now() - started;
    for (let i = 0; i < 4; i++) await new Promise(requestAnimationFrame);
    observer.disconnect();
    const elapsed = frames.length > 1 ? frames.at(-1)!.ms - frames[0].ms : 0;
    return {
      measurement:
        'one page; archived 4,096-point source; local pan and wheel; draw followed by two animation frames',
      target_fps: 30,
      pi_acceptance_claimed: false,
      offered,
      offered_elapsed_ms: offeredElapsed,
      range_changes: changed,
      unique_view_states: new Set(ranges).size,
      acknowledgements: frames.length,
      frames_per_second: elapsed ? ((frames.length - 1) * 1000) / elapsed : 0,
      frames,
      source_id: canvas.dataset.sweepId,
      viewport: { width: innerWidth, height: innerHeight, scale: devicePixelRatio },
      renderer: debug
        ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL)
        : gl.getParameter(gl.RENDERER),
    };
  });
  const files = [
    '../src/spectrum.tsx',
    '../src/spectrum-renderer.ts',
    '../src/spectrum-view.ts',
    '../src/charts.tsx',
    '../src/styles.css',
    '../src/live.ts',
    '../../../radioinspector/web/server.py',
  ];
  const sourceHashes = Object.fromEntries(
    await Promise.all(
      files.map(async (path) => [
        path,
        createHash('sha256')
          .update(await readFile(new URL(path, import.meta.url)))
          .digest('hex'),
      ]),
    ),
  );
  const report = {
    ...result,
    browser: browser.version(),
    node: process.version,
    api_mutations: mutations,
    source_sha256: sourceHashes,
    cpu_quota: await readFile('/sys/fs/cgroup/cpu.max', 'utf8').catch(() => null),
    cpu_before: cpuBefore,
    cpu_after: await readFile('/sys/fs/cgroup/cpu.stat', 'utf8').catch(() => null),
  };
  const output = testInfo.outputPath('interaction-probe.json');
  await writeFile(output, JSON.stringify(report, null, 2));
  await testInfo.attach('interaction-probe', { path: output, contentType: 'application/json' });
  expect(result.source_id).toBe(sourceId);
  expect(mutations).toEqual([]);
  expect(result.offered).toBeGreaterThanOrEqual(150);
  expect(result.range_changes).toBeGreaterThanOrEqual(result.offered - 3);
  expect(result.acknowledgements).toBeGreaterThanOrEqual(result.offered - 3);
  expect(result.frames_per_second).toBeGreaterThanOrEqual(30);
});
