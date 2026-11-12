import { expect, test } from '@playwright/test';
import { openDesk } from './helpers';

test('actual SwiftShader automatically uses Canvas for both charts without an identity fixture', async ({
  page,
}, testInfo) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
  await openDesk(page);
  await page.getByTestId('project-select').selectOption({ label: 'North laboratory' });
  await expect(page.getByTestId('spectrum-renderer-state').first()).toHaveText('Canvas fallback');
  await expect(page.getByTestId('renderer-state').first()).toHaveText('Canvas fallback');
  const chart = page.getByTestId('spectrum-chart').first();
  await expect(chart.locator('canvas.spectrum')).toHaveAttribute('data-sweep-id', /.+/);
  const identity = await chart
    .locator('canvas')
    .first()
    .evaluate((canvas) => {
      const gl = (canvas as HTMLCanvasElement).getContext('webgl2')!;
      const extension = gl.getExtension('WEBGL_debug_renderer_info')!;
      return {
        actual_renderer: gl.getParameter(extension.UNMASKED_RENDERER_WEBGL),
        fixture_present: '__oriTestHardwareIdentityFixture' in window,
        hardware_validation: 'not_run',
      };
    });
  expect(identity.actual_renderer).toMatch(/\bSwiftShader\b/i);
  expect(identity.fixture_present).toBe(false);
  await testInfo.attach('actual-automatic-software-fallback.json', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  });
});
