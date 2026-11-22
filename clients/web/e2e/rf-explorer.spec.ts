import { expect, test } from '@playwright/test';
import { openDesk } from './helpers';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
});

test('an older core keeps the paired monitoring desk when RF Explorer is unavailable', async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-workspace', 'rf-explorer'));
  await page.route('**/api/v1/capabilities', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json();
    delete envelope.data.features.rf_explorer_3g_spectrum;
    await route.fulfill({ response, json: envelope });
  });
  await openDesk(page);
  await expect(page.getByRole('button', { name: 'RF Explorer', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Monitoring', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(page.getByRole('button', { name: 'Arm guard', exact: true })).toBeVisible();
});

test('creates a receive-only RF Explorer stream, plots committed sweeps and reopens original history', async ({
  page,
  request,
}) => {
  await page.route('**/api/v1/capabilities', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json();
    envelope.data.features.rf_explorer_3g_spectrum = true;
    await route.fulfill({ response, json: envelope });
  });
  const envelopes = (data: unknown) => ({
    contract: 'openradioinspector-platform',
    contract_version: 1,
    data,
  });
  const writes: string[] = [];
  const created: {
    id: string;
    streams: {
      id: string;
      device: string;
      role: string;
      port: string;
      level_unit: string;
      settings: Record<string, unknown>;
    }[];
  }[] = [];
  let acquiring = false;
  // The real core owns session state, the archive and the committed event subscription.
  // Intercept every serial worker command before it can open a physical port.
  await page.route(/\/api\/v1\/sessions(?:\/|\?|$)/, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === 'POST') {
      writes.push(path);
      if (path === '/api/v1/sessions') {
        const response = await route.fetch();
        created.push((await response.json()).data);
        return route.fulfill({ response });
      }
      if (
        created.some((session) =>
          ['start', 'stop'].some(
            (action) => path === '/api/v1/sessions/' + session.id + '/' + action,
          ),
        )
      )
        return route.continue();
      const stream = created.at(-1)?.streams[0];
      const prefix = '/api/v1/sessions/' + created.at(-1)?.id + '/streams/' + stream?.id;
      if (path === prefix + '/start' || path === prefix + '/stop') {
        acquiring = path.endsWith('/start');
        return route.fulfill({
          json: envelopes({
            stream_id: stream?.id,
            alive: acquiring,
            state: acquiring ? 'running' : 'disconnected',
          }),
        });
      }
      return route.abort();
    }
    if (path.endsWith('/workers')) {
      const response = await route.fetch();
      const envelope = await response.json();
      for (const worker of envelope.data) {
        if (worker.stream_id === created.at(-1)?.streams[0].id) {
          worker.alive = acquiring;
          worker.state = acquiring ? 'running' : 'disconnected';
        }
      }
      return route.fulfill({ response, json: envelope });
    }
    return route.continue();
  });
  await openDesk(page);
  await page.getByRole('button', { name: 'RF Explorer', exact: true }).click({ timeout: 2500 });
  await expect(page.getByRole('button', { name: 'Arm guard', exact: true })).toHaveCount(0);
  await expect(page.getByText(/tinySA|AR-DV1|generator/i)).toHaveCount(0);
  await page.getByRole('button', { name: 'New spectrum session' }).click();
  await page.getByLabel('Session name').fill('Synthetic RF Explorer current window');
  await page.getByLabel('RF Explorer serial port').fill('COM77');
  await page.getByRole('button', { name: 'Create session', exact: true }).click();
  await expect.poll(() => created.length).toBe(1);
  expect(writes).toEqual(['/api/v1/sessions']);
  expect(created[0].streams).toHaveLength(1);
  expect(created[0].streams[0]).toMatchObject({
    role: 'spectrum_analyzer',
    level_unit: 'dBm',
    device: 'RF Explorer 3G Combo',
    port: 'COM77',
  });
  expect(created[0].streams[0].settings).toEqual({
    adapter: 'rf-explorer-3g-serial',
    baud: 500000,
  });

  await page.getByRole('button', { name: 'New spectrum session' }).click();
  await page.getByLabel('Session name').fill('Synthetic RF Explorer requested window');
  await page.getByLabel('RF Explorer serial port').fill('COM77');
  await page.getByLabel('Set a sweep window').check();
  await page.getByLabel('Start frequency (Hz)').fill('100000000');
  await page.getByLabel('Stop frequency (Hz)').fill('101110500');
  await page.getByRole('button', { name: 'Create session', exact: true }).click();
  expect(created).toHaveLength(1);
  await page.getByLabel('Stop frequency (Hz)').fill('101110000');
  await page.getByRole('button', { name: 'Create session', exact: true }).click();
  await expect.poll(() => created.length).toBe(2);
  expect(created[1].streams[0].settings).toEqual({
    adapter: 'rf-explorer-3g-serial',
    baud: 500000,
    start_hz: 100000000,
    stop_hz: 101110000,
  });
  const session = created[1];
  const stream = session.streams[0];
  const prefix = '/api/v1/sessions/' + session.id;
  await page.getByRole('button', { name: 'Start acquisition', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Stop acquisition', exact: true })).toBeEnabled();
  expect(writes.slice(2)).toEqual([prefix + '/start', prefix + '/streams/' + stream.id + '/start']);
  const source = {
    id: 'rfe-source-' + session.id,
    stream_id: stream.id,
    session_id: session.id,
    interval: { started_at: '2026-11-20T00:00:00Z', ended_at: '2026-11-20T00:00:01Z' },
    frequencies_hz: Array.from({ length: 112 }, (_, i) => 100000000 + i * 10000),
    levels: Array.from({ length: 112 }, (_, i) => (i === 47 ? -43.5 : -100)),
    level_unit: 'dBm',
    settings: {
      adapter: 'rf-explorer-3g-serial',
      rbw_hz: 30000,
      step_hz: 10000,
      detector: 'max_hold',
    },
  };
  const response = await request.post('http://127.0.0.1:18771' + prefix + '/sweeps/batch', {
    data: { items: [source] },
  });
  expect(response.ok()).toBe(true);
  await expect(page.getByTestId('source-id')).toHaveText(source.id);
  await expect(page.getByTestId('spectrum-chart').locator('canvas.spectrum')).toHaveAttribute(
    'data-sweep-id',
    source.id,
  );
  await expect(page.getByRole('img', { name: /Waterfall, 1 committed rows/ })).toBeVisible();
  const plot = page.locator('.spectrum-panel');
  await expect(plot).toContainText('max_hold');
  await expect(plot).toContainText('30,000 Hz');
  await page.getByRole('button', { name: 'Stop acquisition', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Start acquisition', exact: true })).toBeEnabled();
  expect(writes.at(-1)).toBe(prefix + '/streams/' + stream.id + '/stop');

  await page.getByRole('button', { name: 'Finish session', exact: true }).click();
  await expect(page.getByText(/COM77 · Stopped/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start acquisition', exact: true })).toBeDisabled();
  expect(writes.at(-1)).toBe(prefix + '/stop');

  await page.getByRole('button', { name: 'Load history', exact: true }).click();
  await page.getByRole('button', { name: /Original sweep/ }).click();
  const original = page.getByRole('dialog', { name: 'Original sweep' });
  await expect(original).toContainText('112 original points');
  await expect(original).toContainText('-43.5');
  await expect(original).toContainText('max_hold');
  await expect(original).toContainText('30,000 Hz');
  await original.getByRole('button', { name: 'Next points' }).click();
  await expect(original.getByRole('cell', { name: '101110000', exact: true })).toBeVisible();
  await original.getByRole('button', { name: 'Close original' }).click();
  await page.getByRole('button', { name: 'Monitoring', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Arm guard', exact: true })).toBeVisible();
  await expect(page.getByTestId('source-id')).not.toHaveText(source.id);
});

test('keeps all three workspace tabs within a Russian phone viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/api/v1/capabilities', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json();
    envelope.data.features.rf_explorer_3g_spectrum = true;
    await route.fulfill({ response, json: envelope });
  });
  await openDesk(page);
  await page.getByTestId('locale-select').selectOption('ru');
  await expect(page.getByRole('button', { name: 'RF Explorer', exact: true })).toBeVisible();
  for (const family of ['DejaVu Sans', 'Liberation Sans']) {
    const style = await page.addStyleTag({
      content: `:root, body { font-family: "${family}", sans-serif !important; }`,
    });
    const sizes = await page.locator('.workspace-bar nav').evaluate((nav) => ({
      page: document.documentElement.scrollWidth,
      barBottom: nav.parentElement!.getBoundingClientRect().bottom,
      lastTabBottom: nav.lastElementChild!.getBoundingClientRect().bottom,
      lastTab: nav.lastElementChild!.getBoundingClientRect().right,
    }));
    expect(sizes.lastTab, family).toBeLessThanOrEqual(390);
    expect(sizes.lastTabBottom, family).toBeLessThanOrEqual(sizes.barBottom);
    expect(sizes.page, family).toBe(390);
    await style.evaluate((element) => element.remove());
  }
});
