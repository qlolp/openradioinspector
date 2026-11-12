import { expect, test } from '@playwright/test';
import { openDesk } from './helpers';
import { hardwareIdentityForRealGLTests } from './graphics-fixture.mjs';

test.beforeEach(async ({ page }, testInfo) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
  await page.addInitScript(hardwareIdentityForRealGLTests);
  testInfo.annotations.push({
    type: 'test-only hardware identity',
    description:
      'Shader and restoration tests use the real underlying GL context; actual driver is attached separately. No hardware validation.',
  });
});
test.afterEach(async ({ page }, testInfo) => {
  const identity = await page.evaluate(() => (window as any).__oriTestHardwareIdentityFixture);
  await testInfo.attach('actual-gl-renderer-and-test-identity.json', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  });
});

test('WebGL spectrum supports local wheel, pointer and keyboard views with Canvas loss recovery and an unchanged source peak', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await openDesk(page);
  await page.getByTestId('project-select').selectOption({ label: 'North laboratory' });
  const chart = page.getByTestId('spectrum-chart').first();
  await expect(chart.locator('canvas.spectrum')).toHaveAttribute('data-sweep-id', /.+/);
  await expect(page.getByTestId('spectrum-renderer-state').first()).toHaveText('WebGL2');
  const full = await chart.evaluate((node) => ({
    low: Number((node as HTMLElement).dataset.viewLow),
    high: Number((node as HTMLElement).dataset.viewHigh),
  }));
  const source = await page.getByTestId('source-id').textContent();
  const peak = await page.locator('.spectrum-metrics').textContent();
  const mutations: string[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'GET') mutations.push(request.url());
  });
  await chart.getByRole('button', { name: 'Zoom spectrum in', exact: true }).click();
  await expect
    .poll(
      async () =>
        Number(await chart.getAttribute('data-view-high')) -
        Number(await chart.getAttribute('data-view-low')),
    )
    .toBeCloseTo((full.high - full.low) / 2, 2);
  await chart.getByRole('button', { name: 'Pan spectrum right', exact: true }).click();
  const plot = chart.getByRole('group', { name: 'Spectrum view controls', exact: true });
  await plot.focus();
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('ArrowUp');
  for (let i = 0; i < 7; i++) await page.keyboard.press('ArrowRight');
  await expect(
    chart.getByText('Source peak is outside the displayed frequency range', { exact: false }),
  ).toBeVisible();
  const beforeDrag = Number(await chart.getAttribute('data-view-low'));
  const rect = await plot.boundingBox();
  expect(rect).not.toBeNull();
  await page.mouse.move(rect!.x + rect!.width * 0.55, rect!.y + rect!.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(rect!.x + rect!.width * 0.7, rect!.y + rect!.height * 0.5, { steps: 6 });
  await page.mouse.up();
  expect(Number(await chart.getAttribute('data-view-low'))).toBeLessThan(beforeDrag);
  const width =
    Number(await chart.getAttribute('data-view-high')) -
    Number(await chart.getAttribute('data-view-low'));
  await page.mouse.wheel(0, -150);
  await expect
    .poll(
      async () =>
        Number(await chart.getAttribute('data-view-high')) -
        Number(await chart.getAttribute('data-view-low')),
    )
    .toBeLessThan(width);
  await plot.focus();
  await page.keyboard.press('Home');
  await expect(chart).toHaveAttribute('data-view-low', String(full.low));
  await expect(chart).toHaveAttribute('data-view-high', String(full.high));
  const glAvailable = await chart.locator('canvas.spectrum').evaluate((canvas) => {
    const extension = (canvas as HTMLCanvasElement)
      .getContext('webgl2')
      ?.getExtension('WEBGL_lose_context');
    (window as any).__restoreSpectrum = () => extension?.restoreContext();
    extension?.loseContext();
    return !!extension;
  });
  expect(glAvailable).toBe(true);
  await expect(page.getByTestId('spectrum-renderer-state').first()).toContainText(
    'Canvas fallback',
  );
  await expect(chart.locator('canvas.spectrum')).toHaveAttribute('data-sweep-id', source!);
  await page.screenshot({ path: 'test-results/spectrum-canvas-recovery.png', fullPage: true });
  await page.evaluate(() => (window as any).__restoreSpectrum());
  await expect(page.getByTestId('spectrum-renderer-state').first()).toHaveText('WebGL2');
  await expect(chart.locator('canvas.spectrum')).toHaveAttribute('data-sweep-id', source!);
  expect(await page.locator('.spectrum-metrics').textContent()).toBe(peak);
  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
  await page.screenshot({ path: 'test-results/spectrum-webgl-local-view.png', fullPage: true });
});

test('active GL waterfall uploads incremental rows without maintaining a hidden Canvas history', async ({
  page,
  request,
}) => {
  await page.addInitScript(() => {
    const context = CanvasRenderingContext2D.prototype;
    const draw = context.drawImage,
      put = context.putImageData;
    (window as any).__hiddenWaterfallCopies = 0;
    for (const [name, original] of [
      ['drawImage', draw],
      ['putImageData', put],
    ] as const) {
      (context as any)[name] = function (...args: any[]) {
        if (this.canvas.closest('.waterfall') && this.canvas.hidden)
          (window as any).__hiddenWaterfallCopies++;
        return (original as any).apply(this, args);
      };
    }
  });
  await openDesk(page);
  await page.getByTestId('project-select').selectOption({ label: 'North laboratory' });
  await expect(page.getByTestId('renderer-state')).toContainText('WebGL2');
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute('data-sweep-id', /.+/);
  const base = 'http://127.0.0.1:18771/api/v1';
  const project = (await (await request.get(base + '/monitoring-projects')).json()).data.items.find(
    (p: any) => p.name === 'North laboratory',
  );
  const source = (
    await (
      await request.get(base + '/sessions/' + project.active_session_id + '/sweeps?limit=1')
    ).json()
  ).data[0];
  for (let i = 0; i < 4; i++) {
    const response = await request.post(base + '/sessions/' + source.session_id + '/sweeps', {
      data: { ...source, id: 'gl-incremental-' + i },
    });
    expect(response.ok()).toBe(true);
    await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
      'data-sweep-id',
      'gl-incremental-' + i,
    );
  }
  expect(await page.evaluate(() => (window as any).__hiddenWaterfallCopies)).toBe(0);
  await page
    .locator('.waterfall canvas')
    .first()
    .evaluate((canvas) =>
      (canvas as HTMLCanvasElement)
        .getContext('webgl2')!
        .getExtension('WEBGL_lose_context')!
        .loseContext(),
    );
  await expect(page.getByTestId('renderer-state')).toContainText('Canvas fallback');
  const pixel = await page
    .locator('.waterfall canvas:visible')
    .evaluate((canvas) =>
      Array.from((canvas as HTMLCanvasElement).getContext('2d')!.getImageData(0, 0, 1, 1).data),
    );
  expect(pixel[3]).toBe(255);
});
