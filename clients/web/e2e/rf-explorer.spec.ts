import { expect, test, type Page } from '@playwright/test';
import { openDesk } from './helpers';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
});

async function enableRfExplorer(page: Page, survey = false) {
  await page.route('**/api/v1/capabilities', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json();
    envelope.data.features.rf_explorer_3g_spectrum = true;
    if (survey) envelope.data.features.rf_explorer_10khz_survey = true;
    else delete envelope.data.features.rf_explorer_10khz_survey;
    await route.fulfill({ response, json: envelope });
  });
}

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
  await enableRfExplorer(page);
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
  await expect(page.getByRole('checkbox', { name: 'Set a sweep window' })).toBeVisible();
  await expect(page.getByRole('option', { name: /10 kHz survey/ })).toHaveCount(0);
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

test('creates negotiated surveys and shows the measured segment within the planned range', async ({
  page,
  request,
}) => {
  await enableRfExplorer(page, true);
  await openDesk(page);
  await page.getByRole('button', { name: 'RF Explorer', exact: true }).click();
  const openSession = async (mode: string) => {
    await page.getByRole('button', { name: 'New spectrum session' }).click();
    await page.getByLabel('Session name').fill(mode);
    await page.getByLabel('RF Explorer serial port').fill('COM77');
    await page.getByLabel('Acquisition mode').selectOption({ label: mode });
  };
  const createSession = async () => {
    const created = page.waitForResponse(
      (r) => new URL(r.url()).pathname === '/api/v1/sessions' && r.request().method() === 'POST',
    );
    await page.getByRole('button', { name: 'Create session', exact: true }).click();
    const response = await created;
    expect(response.ok()).toBe(true);
    return (await response.json()).data;
  };
  await openSession('10 kHz survey · full active module');
  await expect(page.getByLabel('Start frequency (Hz)')).toHaveCount(0);
  const full = await createSession();
  expect(full.streams[0].settings).toEqual({
    adapter: 'rf-explorer-3g-serial',
    baud: 500000,
    survey_step_hz: 10000,
  });

  await openSession('10 kHz survey · selected range');
  await page.getByLabel('Start frequency (Hz)').fill('200001000');
  await page.getByLabel('Stop frequency (Hz)').fill('202230000');
  await page.getByRole('button', { name: 'Create session', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('10 kHz steps');
  await page.getByLabel('Stop frequency (Hz)').fill('202231000');
  const session = await createSession();
  expect(session.streams[0].settings).toEqual({
    adapter: 'rf-explorer-3g-serial',
    baud: 500000,
    survey_step_hz: 10000,
    start_hz: 200001000,
    stop_hz: 202231000,
  });
  await expect(page.getByRole('button', { name: 'Start acquisition', exact: true })).toBeEnabled();

  const base = 'http://127.0.0.1:18771/api/v1/sessions/' + session.id;
  expect((await request.post(base + '/start', { data: {} })).ok()).toBe(true);
  const source = {
    id: 'segment-' + session.id,
    session_id: session.id,
    stream_id: session.streams[0].id,
    interval: { started_at: '2026-11-20T00:00:00Z', ended_at: '2026-11-20T00:00:01Z' },
    frequencies_hz: Array.from({ length: 112 }, (_, i) => 201121000 + i * 10000),
    levels: Array(112).fill(-75),
    level_unit: 'dBm',
    settings: {
      adapter: 'rf-explorer-3g-serial',
      detector: 'normal',
      rbw_hz: 15000,
      step_hz: 10000,
      survey: {
        start_hz: 200001000,
        stop_hz: 202231000,
        step_hz: 10000,
        pass: 3,
        tile: 2,
        tiles: 2,
        pass_id: 'synthetic-pass-3',
      },
    },
  };
  const firstSegment = {
    ...source,
    id: source.id + '-first',
    interval: { started_at: '2026-11-19T23:59:58Z', ended_at: '2026-11-19T23:59:59Z' },
    frequencies_hz: Array.from({ length: 112 }, (_, i) => 200001000 + i * 10000),
    settings: { ...source.settings, survey: { ...source.settings.survey, tile: 1 } },
  };
  const ingest = await request.post(base + '/sweeps/batch', {
    data: { items: [firstSegment, source] },
  });
  expect(ingest.ok()).toBe(true);
  const plot = page.locator('.spectrum-panel');
  await expect(plot.getByTestId('source-id')).toHaveText(source.id);
  await expect(plot).toContainText('Pass 3 · segment 2 of 2');
  await expect(plot.getByText('Planned survey range', { exact: true }).locator('+ dd')).toHaveText(
    '200,001,000 Hz – 202,231,000 Hz',
  );
  await expect(plot.getByText('Measured segment', { exact: true }).locator('+ dd')).toHaveText(
    '201,121,000 Hz – 202,231,000 Hz',
  );
  const spectrum = plot.getByTestId('spectrum-chart');
  await expect(spectrum).toHaveAttribute('data-view-low', '201121000');
  await expect(spectrum).toHaveAttribute('data-view-high', '202231000');
  const waterfall = plot.getByRole('img', { name: /Waterfall, 2 committed rows/ });
  const edges = await waterfall.locator('canvas:visible').evaluate((element) => {
    const canvas = element as HTMLCanvasElement;
    const ctx = canvas.getContext('2d')!;
    return [0, 1].map((y) =>
      [0, canvas.width - 1].map((x) => Array.from(ctx.getImageData(x, y, 1, 1).data)),
    );
  });
  const black = [0, 0, 0, 255];
  expect(edges[0][0]).toEqual(black);
  expect(edges[0][1]).not.toEqual(black);
  expect(edges[1][0]).not.toEqual(black);
  expect(edges[1][1]).toEqual(black);
  await page.getByRole('button', { name: 'Load history', exact: true }).click();
  await page.getByRole('button', { name: 'Original sweep · ' + source.id, exact: true }).click();
  const original = page.getByRole('dialog', { name: 'Original sweep' });
  await expect(original).toContainText('Pass 3 · segment 2 of 2');
  await expect(original).toContainText('synthetic-pass-3');
  await expect(original.getByText('Measured segment', { exact: true }).locator('+ dd')).toHaveText(
    '201,121,000 Hz – 202,231,000 Hz',
  );
  await original.getByRole('button', { name: 'Close original' }).click();
  await page.getByTestId('locale-select').selectOption('ru');
  await expect(plot).toContainText('Проход 3 · участок 2 из 2');
});

test('keeps a saved survey readable when the connected core lacks survey acquisition', async ({
  page,
  request,
}) => {
  const response = await request.post('http://127.0.0.1:18771/api/v1/sessions', {
    data: {
      name: 'Saved module survey',
      streams: [
        {
          device: 'RF Explorer 3G Combo',
          port: 'COM77',
          role: 'spectrum_analyzer',
          level_unit: 'dBm',
          settings: { adapter: 'rf-explorer-3g-serial', baud: 500000, survey_step_hz: 10000 },
        },
      ],
    },
  });
  expect(response.ok()).toBe(true);
  const session = (await response.json()).data;
  await enableRfExplorer(page);
  await openDesk(page);
  await page.getByRole('button', { name: 'RF Explorer', exact: true }).click();
  await page.getByLabel('Spectrum session').selectOption(session.id);
  await expect(
    page.getByText('Starting this survey requires a core with 10 kHz survey support.'),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start acquisition', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Load history' }).click();
  await expect(page.getByText('No saved sweeps for this stream')).toBeVisible();
});

test('keeps the workspace tabs and survey form within a Russian phone viewport', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enableRfExplorer(page, true);
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
  await page.getByRole('button', { name: 'RF Explorer', exact: true }).click();
  await page.getByRole('button', { name: 'Новый сеанс измерения спектра' }).click();
  const form = page.getByRole('dialog', { name: 'Новый сеанс измерения спектра' });
  await form.getByLabel('Режим измерения').selectOption({
    label: 'Обзор с шагом 10 кГц · выбранный диапазон',
  });
  await expect(form.getByRole('spinbutton')).toHaveCount(2);
  expect(await form.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
});
