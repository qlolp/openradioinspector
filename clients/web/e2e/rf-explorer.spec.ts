import { expect, test, type Page } from '@playwright/test';
import { openDesk } from './helpers';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
});

async function enableRfExplorer(page: Page, survey = false, panorama = false) {
  await page.route('**/api/v1/capabilities', async (route) => {
    const response = await route.fetch();
    const envelope = await response.json();
    envelope.data.features.rf_explorer_3g_spectrum = true;
    envelope.data.features.rf_explorer_survey_panorama = panorama;
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
    const name = await page.getByLabel('Session name').inputValue();
    const created = page.waitForResponse(
      (r) => new URL(r.url()).pathname === '/api/v1/sessions' && r.request().method() === 'POST',
    );
    await page.getByRole('button', { name: 'Create session', exact: true }).click();
    const response = await created;
    expect(response.ok()).toBe(true);
    const selected = page.getByLabel('Spectrum session');
    await expect(selected.locator('option:checked')).toHaveText(name);
    const saved = await request.get(
      'http://127.0.0.1:18771/api/v1/sessions/' + encodeURIComponent(await selected.inputValue()),
    );
    expect(saved.ok()).toBe(true);
    return (await saved.json()).data;
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

test('previews and saves offset survey presets before any device command', async ({ page }) => {
  await enableRfExplorer(page, true, true);
  const commands: string[] = [];
  page.on('request', (r) => {
    if (r.method() !== 'GET' && r.url().includes('/streams/')) commands.push(r.url());
  });
  await openDesk(page);
  await page.getByRole('button', { name: 'RF Explorer', exact: true }).click();
  await page.getByRole('button', { name: 'New spectrum session' }).click();
  const form = page.getByRole('dialog');
  await form.getByLabel('Session name').fill('Preview station');
  await form.getByLabel('RF Explorer serial port').fill('COM21');
  await form.getByLabel('Acquisition mode').selectOption('survey-range');
  await form.getByLabel('Range preset').selectOption({ label: '80–120 MHz' });
  await expect(form).toContainText('36 windows');
  await expect(form).toContainText('4,001 unique frequencies');
  await form.getByLabel('Range preset').selectOption({ label: '100–400 MHz' });
  await expect(form).toContainText('268 windows');
  await expect(form).toContainText('Right SMA');
  await form.getByLabel('Frequency entry').selectOption('center');
  await expect(form.getByLabel('Center frequency (Hz)')).toHaveValue('250000000');
  await form.getByLabel('Center frequency (Hz)').fill('101116000');
  await form.getByLabel('Span (Hz)').fill('2230000');
  await form.getByLabel('Frequency entry').selectOption('edges');
  await expect(form.getByLabel('Start frequency (Hz)')).toHaveValue('100001000');
  await expect(form.getByLabel('Stop frequency (Hz)')).toHaveValue('102231000');
  await form.getByLabel('Preset name').fill('Offset receiver');
  await form.getByRole('button', { name: 'Save preset locally' }).click();
  await form.getByLabel('Range preset').selectOption({ label: 'Full active board' });
  await expect(form.getByLabel('Start frequency (Hz)')).toHaveCount(0);
  await expect(form).toContainText('2,398 windows');
  await expect(form).toContainText('643 windows');
  await form.getByLabel('Range preset').selectOption({ label: 'Offset receiver' });
  await expect(form.getByLabel('Start frequency (Hz)')).toHaveValue('100001000');
  expect(commands).toHaveLength(0);
});

for (const stopped of [false, true])
  test(
    'reviews sparse passes, opens the clicked source and plays originals with ' +
      (stopped ? 'stopped' : 'active') +
      ' acquisition',
    async ({ page, request }) => {
      await enableRfExplorer(page, true, true);
      const api = 'http://127.0.0.1:18771/api/v1/sessions';
      const make = await request.post(api, {
        data: {
          name: 'Pass review fixture',
          streams: [
            {
              device: 'RF Explorer 3G Combo',
              role: 'spectrum_analyzer',
              level_unit: 'dBm',
              port: 'COM21',
              settings: { adapter: 'rf-explorer-3g-serial', survey_step_hz: 10000 },
            },
          ],
        },
      });
      const session = (await make.json()).data,
        stream = session.streams[0];
      const start = '2026-11-22T10:05:00.000000Z',
        end = '2026-11-22T10:05:00.100000Z';
      const next = '2026-11-22T10:05:00.500000Z',
        last = '2026-11-22T10:05:00.750000Z';
      const id = 'rf-pass',
        base = api + '/' + session.id;
      const bins = stopped ? 672 : 336,
        tileCount = stopped ? 6 : 3,
        upper = 80000000 + (bins - 1) * 10000;
      const source = (tile: number, low: number, started_at: string, ended_at: string) => ({
        id: session.id + '-tile-' + tile,
        session_id: session.id,
        stream_id: stream.id,
        interval: { started_at, ended_at },
        frequencies_hz: Array.from({ length: 112 }, (_, i) => low + i * 10000),
        levels: Array.from({ length: 112 }, (_, i) =>
          i === 50 ? (tile === 1 ? -60 : -40.5) : -90,
        ),
        level_unit: 'dBm',
        settings: {
          adapter: 'rf-explorer-3g-serial',
          active_board: 'expansion',
          active_input: 'right',
          board_min_hz: 15000000,
          board_max_hz: 2700000000,
          rbw_hz: 15000,
          detector: 'max_hold',
          offset_db: -2,
          step_hz: 10000,
          simulated: true,
          survey: {
            start_hz: 80000000,
            stop_hz: upper,
            step_hz: 10000,
            pass_id: id,
            pass: 2,
            tile,
            tiles: tileCount,
          },
        },
      });
      const originals = [source(1, 80000000, start, end), source(3, 82240000, next, last)];
      expect((await request.post(base + '/start', { data: {} })).ok()).toBe(true);
      expect(
        (await request.post(base + '/sweeps/batch', { data: { items: originals } })).ok(),
      ).toBe(true);
      if (stopped) expect((await request.post(base + '/stop', { data: {} })).ok()).toBe(true);
      await page.route('**/sessions/' + session.id + '/workers', async (route) => {
        const response = await route.fetch(),
          envelope = await response.json();
        envelope.data.forEach((w: { alive: boolean; state: string }) => {
          w.alive = !stopped;
          w.state = stopped ? 'disconnected' : 'running';
        });
        await route.fulfill({ response, json: envelope });
      });
      const prior = {
        ...originals[0],
        id: session.id + '-previous-pass',
        interval: {
          started_at: '2026-11-22T10:04:51.000000Z',
          ended_at: '2026-11-22T10:04:52.000000Z',
        },
        settings: {
          ...originals[0].settings,
          survey: { ...originals[0].settings.survey, pass: 1, pass_id: 'earlier' },
        },
        levels: originals[0].levels.map((v, i) => (i === 51 ? -30.5 : i === 50 ? -20 : v)),
      };
      expect(
        (
          await request.post(base + '/frequency-bookmarks', {
            data: { name: 'Quiet bin', entries: [{ frequency_hz: 80510000, label: 'Quiet bin' }] },
          })
        ).ok(),
      ).toBe(true);
      const summary = {
        pass_id: id,
        pass: 2,
        start_hz: 80000000,
        stop_hz: upper,
        step_hz: 10000,
        tiles: tileCount,
        received_tiles: 2,
        unique_points: 224,
        expected_points: bins,
        started_at: start,
        ended_at: last,
        complete: false,
        duration_s: null,
        revisit_s: 7.25,
        active_board: 'expansion',
        active_input: 'right',
        rbw_hz: 15000,
        detector: 'max_hold',
        offset_db: -2,
        simulated: true,
      };
      const holds: (string | null)[] = [],
        commands: string[] = [],
        windows: number[] = [];
      const envelope = (data: unknown) => ({
        data,
        contract: 'openradioinspector-platform',
        contract_version: 1,
      });
      await page.route(/\/rf-explorer\/passes(?:[/?]|$)/, async (route) => {
        const url = new URL(route.request().url());
        if (url.pathname.endsWith('/point')) {
          const f = Number(url.searchParams.get('frequency_hz'));
          const sw = originals.find((w) => w.frequencies_hz.includes(f));
          const i = sw?.frequencies_hz.indexOf(f) ?? -1;
          const p = sw && {
            tile: sw.settings.survey.tile,
            point_index: i,
            frequency_hz: f,
            sweep_id: sw.id,
            level_dbm: sw.levels[i],
            ...sw.interval,
            last_measured_at: sw.interval.ended_at,
          };
          const held = url.searchParams.get('hold_since');
          return route.fulfill({
            json: envelope({
              current: p ?? null,
              held:
                held?.startsWith('1970-') && f === 80510000
                  ? {
                      ...p,
                      sweep_id: prior.id,
                      level_dbm: -30.5,
                      ...prior.interval,
                      last_measured_at: originals[0].interval.ended_at,
                    }
                  : null,
            }),
          });
        }
        if (url.pathname.endsWith('/windows')) {
          const after = Number(url.searchParams.get('after_tile') || 0);
          windows.push(after);
          const sw = originals[after === 0 ? 0 : 1];
          return route.fulfill({
            json: envelope({
              items: [{ tile: sw.settings.survey.tile, sweep_id: sw.id, ...sw.interval }],
              next_after_tile: after === 0 ? 1 : null,
            }),
          });
        }
        if (url.pathname.endsWith('/passes'))
          return route.fulfill({ json: envelope({ items: [summary], next_before: null }) });
        const count = Number(url.searchParams.get('columns')),
          held = url.searchParams.get('hold_since');
        expect(count).toBeGreaterThan(0);
        expect(count).toBeLessThanOrEqual(2048);
        holds.push(held);
        const columns = Array(count).fill(null);
        for (const sw of originals)
          for (let i = 0; i < 112; i++) {
            const frequency_hz = sw.frequencies_hz[i];

            const column = Math.min(
              count - 1,
              Math.floor(((frequency_hz - 80000000) * count) / (bins * 10000)),
            );
            if (columns[column] && columns[column].level_dbm >= sw.levels[i]) continue;
            columns[column] = {
              tile: sw.settings.survey.tile,
              frequency_hz,
              level_dbm: sw.levels[i],
              point_index: i,
              sweep_id: sw.id,
              ...sw.interval,
              last_measured_at: sw.interval.ended_at,
            };
          }
        const coverage = [
          { start_hz: 80000000, stop_hz: 81110000 },
          { start_hz: 82240000, stop_hz: 83350000 },
        ];
        const heldPoints = held?.startsWith('1970-')
          ? columns.map((p) => (p ? { ...p } : null))
          : null;
        if (heldPoints)
          for (let i = 0; i < prior.levels.length; i++) {
            const f = prior.frequencies_hz[i],
              col = Math.min(count - 1, Math.floor(((f - 80000000) * count) / (bins * 10000)));
            if (!heldPoints[col] || prior.levels[i] > heldPoints[col].level_dbm)
              heldPoints[col] = {
                frequency_hz: f,
                point_index: i,
                tile: 1,
                sweep_id: prior.id,
                level_dbm: prior.levels[i],
                ...prior.interval,
                last_measured_at: end,
              };
          }
        return route.fulfill({
          json: envelope({
            summary,
            columns,
            coverage,
            hold_columns: held ? (heldPoints ?? Array(count).fill(null)) : null,
            hold_coverage: held ? (heldPoints ? coverage : []) : null,
          }),
        });
      });
      await page.route('**/sweeps/*', async (route) => {
        const id = decodeURIComponent(new URL(route.request().url()).pathname.split('/').at(-1)!);
        const sw = [...originals, prior].find((source) => source.id === id);
        return sw ? route.fulfill({ json: envelope(sw) }) : route.continue();
      });
      page.on('request', (r) => {
        if (
          r.method() !== 'GET' &&
          r.url().includes('/api/v1/sessions/') &&
          !r.url().endsWith('/frequency-bookmarks')
        )
          commands.push(r.url());
      });
      await openDesk(page);
      await page.getByRole('button', { name: 'RF Explorer', exact: true }).click();
      await page.getByLabel('Spectrum session').selectOption(session.id);
      await page.getByRole('button', { name: 'Entire pass', exact: true }).click();
      const pass = page.getByRole('region', { name: 'Survey pass panorama' });
      await expect(pass).toContainText('224 / ' + bins);
      await expect(pass).toContainText('7.25');
      await expect(pass).toContainText('max_hold');
      await pass.getByText('Unmeasured gaps', { exact: true }).click();
      await expect(pass).toContainText('81,120,000 Hz – 82,230,000 Hz');
      const chart = pass.getByRole('group', { name: /Survey spectrum in dBm/ });
      const box = await chart.boundingBox();
      await chart.click({ position: { x: (box!.width * 167) / (bins - 1), y: box!.height / 2 } });
      await expect(pass.getByText('No original point at this frequency')).toBeVisible();
      await chart.click({ position: { x: (box!.width * 274) / (bins - 1), y: box!.height / 2 } });
      await expect(pass).toContainText('82,740,000 Hz');
      await pass.getByRole('button', { name: 'Select strongest point' }).click();
      await expect(pass).toContainText('82,740,000 Hz');
      await expect(pass).toContainText('-40.5 dBm');
      await page.getByTestId('locale-select').selectOption('ru');
      const panel = page.locator('.spectrum-panel');
      await panel.evaluate((el) => el.scrollIntoView({ block: 'start' }));
      const capture = await panel.boundingBox();
      await page.screenshot({
        path: test.info().outputPath('rf-explorer-pass-ru.png'),
        clip: {
          x: capture!.x,
          y: Math.max(0, capture!.y),
          width: capture!.width,
          height: Math.min(capture!.height, 940),
        },
      });
      await page.setViewportSize({ width: 390, height: 844 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.getByTestId('locale-select').selectOption('en');
      await pass.getByLabel('Marker name').fill('Test carrier');
      await pass.getByRole('button', { name: 'Save marker' }).click();
      await expect(
        pass.getByLabel('Saved frequency markers').locator('option', { hasText: 'Test carrier' }),
      ).toHaveCount(1);
      await pass.getByRole('button', { name: 'Open original sweep' }).click();
      const original = page.getByRole('dialog', { name: 'Original sweep' });
      await expect(original).toContainText(originals[1].id);
      await expect(original.getByRole('row', { name: '50 82740000 -40.5' })).toHaveAttribute(
        'aria-current',
        'true',
      );
      await original.getByRole('button', { name: 'Close original' }).click();
      const quiet = pass
        .getByLabel('Saved frequency markers')
        .locator('option', { hasText: 'Quiet bin' });
      await pass
        .getByLabel('Saved frequency markers')
        .selectOption((await quiet.getAttribute('value'))!);
      await expect(pass.locator('.rf-evidence')).toContainText('80,510,000 Hz');
      await expect(pass.locator('.rf-evidence')).toContainText('-90 dBm');
      await pass.getByRole('button', { name: 'Open original sweep' }).click();
      await expect(original.getByRole('row', { name: '51 80510000 -90' })).toHaveAttribute(
        'aria-current',
        'true',
      );
      await original.getByRole('button', { name: 'Close original' }).click();
      await pass.getByRole('button', { name: 'Enable archive Max Hold' }).click();
      await pass.getByLabel('Inspect trace').selectOption('hold');
      await pass.getByLabel('Saved frequency markers').selectOption('');
      await pass
        .getByLabel('Saved frequency markers')
        .selectOption((await quiet.getAttribute('value'))!);
      await expect(pass.locator('.rf-evidence')).toContainText(prior.id);
      await expect(pass.locator('.rf-evidence')).toContainText('-30.5 dBm');
      await expect(pass.locator('.rf-evidence')).toContainText('10:04:52');
      await expect(pass.locator('.rf-evidence')).toContainText('10:05:00.100000');
      await pass.getByRole('button', { name: 'Open original sweep' }).click();
      await expect(original.getByRole('row', { name: '51 80510000 -30.5' })).toHaveAttribute(
        'aria-current',
        'true',
      );
      await original.getByRole('button', { name: 'Close original' }).click();
      await page.getByRole('button', { name: 'Pause display' }).click();
      await expect(
        page.getByText('Display paused. Acquisition continues independently.'),
      ).toBeVisible();
      const previousReads = holds.length;
      if (!stopped)
        expect(
          (
            await request.post(base + '/sweeps/batch', {
              data: { items: [{ ...originals[1], id: session.id + '-later' }] },
            })
          ).ok(),
        ).toBe(true);
      await expect(page.getByRole('button', { name: 'Resume display' })).toBeVisible();
      expect(holds.length).toBe(previousReads);
      await page.getByRole('button', { name: 'Resume display' }).click();
      expect(holds).toContain(null);
      await pass
        .getByRole('button', {
          name: stopped ? 'Reset Max Hold to start of this pass' : 'Reset software Max Hold',
        })
        .click();
      await expect
        .poll(() => holds.some((x) => x && !x.startsWith('1970-') && x.endsWith('Z')))
        .toBe(true);
      if (stopped) expect(holds.at(-1)).toBe(start);
      const reset = holds.at(-1);
      await pass.getByRole('button', { name: 'Hide software Max Hold' }).click();
      await pass.getByRole('button', { name: 'Show software Max Hold' }).click();
      await expect.poll(() => holds.at(-1)).toBe(reset);
      await pass.getByRole('button', { name: 'Review original windows' }).click();
      const review = pass.getByRole('group', { name: 'Original window playback' });
      await expect(review).toContainText(originals[0].id);
      await review.getByRole('button', { name: 'Next sweep' }).click();
      await expect(review).toContainText(originals[1].id);
      await review.getByRole('button', { name: 'Previous sweep' }).click();
      await expect(review).toContainText(originals[0].id);
      await review.getByRole('button', { name: 'Play at recorded speed' }).click();
      await expect(review).toContainText(originals[1].id);
      expect(windows).toContain(1);
      expect(commands).toEqual([]);
    },
  );

test('opens an archived survey on a core with panorama support and serial acquisition disabled', async ({
  page,
  request,
}) => {
  const created = await request.post('http://127.0.0.1:18771/api/v1/sessions', {
    data: {
      name: 'Archive-only station',
      streams: [
        {
          device: 'RF Explorer 3G Combo',
          port: 'COM42',
          role: 'spectrum_analyzer',
          level_unit: 'dBm',
          settings: { adapter: 'rf-explorer-3g-serial', survey_step_hz: 10000 },
        },
      ],
    },
  });
  const s = (await created.json()).data,
    calls: string[] = [];
  await page.route('**/api/v1/capabilities', async (route) => {
    const response = await route.fetch(),
      envelope = await response.json();
    Object.assign(envelope.data.features, {
      rf_explorer_3g_spectrum: false,
      rf_explorer_10khz_survey: false,
      rf_explorer_survey_panorama: true,
    });
    return route.fulfill({ response, json: envelope });
  });
  await page.route(/\/rf-explorer\/passes(?:[/?]|$)/, (route) =>
    route.fulfill({
      json: {
        contract: 'openradioinspector-platform',
        contract_version: 1,
        data: { items: [], next_before: null },
      },
    }),
  );
  page.on('request', (r) => {
    if (r.url().includes(s.id + '/workers') || (r.method() !== 'GET' && r.url().includes(s.id)))
      calls.push(r.url());
  });
  await openDesk(page);
  await page.getByRole('button', { name: 'RF Explorer', exact: true }).click();
  await page.getByLabel('Spectrum session').selectOption(s.id);
  await expect(page.getByRole('button', { name: 'New spectrum session' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Start acquisition' })).toBeDisabled();
  await page.getByRole('button', { name: 'Entire pass', exact: true }).click();
  await expect(page.getByText('No archived survey passes')).toBeVisible();
  expect(calls).toEqual([]);
});

test('paired reader sees a newly committed raw window and its original with operator domains advertised', async ({
  page,
  context,
  request,
}) => {
  const origin = 'https://reader.synthetic.invalid';
  const response = await request.post('http://127.0.0.1:18771/api/v1/sessions', {
    data: {
      name: 'Paired archive reader',
      streams: [
        {
          device: 'RF Explorer 3G Combo',
          port: 'COM81',
          role: 'spectrum_analyzer',
          level_unit: 'dBm',
          settings: { adapter: 'rf-explorer-3g-serial', survey_step_hz: 10000 },
        },
      ],
    },
  });
  const session = (await response.json()).data,
    stream = session.streams[0];
  const source = {
    id: 'reader-live-original',
    session_id: session.id,
    stream_id: stream.id,
    interval: {
      started_at: '2026-11-22T00:00:00.000000Z',
      ended_at: '2026-11-22T00:00:01.000000Z',
    },
    frequencies_hz: Array.from({ length: 112 }, (_, i) => 80000000 + i * 10000),
    levels: Array.from({ length: 112 }, (_, i) => (i === 1 ? -42 : -72)),
    level_unit: 'dBm',
    settings: {
      adapter: 'rf-explorer-3g-serial',
      settings_source: 'device_readback',
      settings_readback: true,
      active_board: 'expansion',
      active_input: 'right',
      board_min_hz: 15000000,
      board_max_hz: 2700000000,
      step_hz: 10000,
      rbw_hz: 15000,
      simulated: true,
    },
  };
  const later = {
    ...source,
    id: 'reader-later-window',
    interval: {
      started_at: '2026-11-22T00:00:04.000000Z',
      ended_at: '2026-11-22T00:00:05.000000Z',
    },
    settings: {
      adapter: 'rf-explorer-3g-serial',
      settings_source: 'request',
      active_board: 'main',
      active_input: 'left',
      board_min_hz: 240000000,
      board_max_hz: 960000000,
    },
  };
  const envelope = (data: unknown) => ({
    contract: 'openradioinspector-platform',
    contract_version: 1,
    data,
  });
  let release!: () => void,
    nextWindow!: () => void,
    subscribed = false;
  const nextReady = new Promise<void>((r) => {
    nextWindow = r;
  });
  const windowReady = new Promise<void>((r) => {
    release = r;
  });
  const denied: string[] = [],
    reads: string[] = [];
  await context.addCookies([
    {
      name: 'ori_session',
      value: 'synthetic-reader',
      url: origin + '/api',
      httpOnly: true,
      secure: true,
    },
  ]);
  await page.addInitScript(() =>
    localStorage.setItem('ori-modern-project', 'cached-operator-project'),
  );
  await context.route(origin + '/**', async (route) => {
    const url = new URL(route.request().url()),
      p = url.pathname;
    if (p.startsWith('/api/')) {
      expect(route.request().headers().cookie).toContain('ori_session=synthetic-reader');
      reads.push(p);
      if (p === '/api/v1/security/status')
        return route.fulfill({
          json: envelope({ mode: 'lan_tls', access_scope: 'reader', pairing_required: false }),
        });
      if (p === '/api/v1/capabilities')
        return route.fulfill({
          json: envelope({
            features: {
              rf_explorer_3g_spectrum: true,
              rf_explorer_survey_panorama: true,
              monitoring_projects: true,
              rf_explorer_band_power: true,
              sweep_tolerance_envelope: true,
              frequency_bookmarks: true,
            },
          }),
        });
      if (p === '/api/v1/health')
        return route.fulfill({
          json: envelope({ status: 'ok', last_event_id: 10, sessions: 1, running_sessions: 1 }),
        });
      if (p === '/api/v1/sessions') return route.fulfill({ json: envelope([session]) });
      if (p.endsWith('/sweeps')) return route.fulfill({ json: envelope([]) });
      if (p.endsWith('/sweeps/' + source.id)) return route.fulfill({ json: envelope(source) });
      if (p === '/api/v1/events') {
        subscribed = true;
        const sequence = Number(url.searchParams.get('after')) + 1;
        if (sequence > 12)
          return route.fulfill({ contentType: 'text/event-stream', body: ': heartbeat\n\n' });
        await (sequence === 11 ? windowReady : nextReady);
        return route.fulfill({
          contentType: 'text/event-stream',
          body:
            'event: sweep.received\nid: ' +
            sequence +
            '\ndata: ' +
            JSON.stringify(
              envelope({
                sequence,
                event_type: 'sweep.received',
                payload: { sweep: sequence === 11 ? source : later },
              }),
            ) +
            '\n\n',
        });
      }
      denied.push(p);
      return route.fulfill({
        status: 403,
        json: { error: { code: 'insufficient_scope', message: 'reader' } },
      });
    }
    return route.fulfill({
      response: await route.fetch({ url: 'http://127.0.0.1:18772' + p + url.search }),
    });
  });
  await page.goto(origin + '/modern/');
  await expect(page.getByRole('button', { name: 'RF Explorer', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'RF Explorer', exact: true }).click();
  try {
    await expect.poll(() => subscribed).toBe(true);
  } finally {
    release();
  }
  await expect(page.getByTestId('source-id')).toHaveText(source.id);
  await expect(page.locator('canvas.spectrum:visible')).toHaveAttribute('data-sweep-id', source.id);
  await page.getByRole('group', { name: 'Spectrum view controls' }).press('Enter');
  await expect(page.locator('.rf-evidence')).toContainText(source.id);
  nextWindow();
  await expect(page.getByTestId('source-id')).toHaveText(later.id);
  await expect(page.locator('.rf-evidence')).toContainText(source.id);
  await expect(page.locator('.rf-evidence')).toContainText('00:00:05.000000 UTC');
  await page.getByText('Acquisition preview', { exact: true }).click();
  await expect(page.locator('.rf-draft-preview')).toContainText('Last simulated input');
  await expect(page.locator('.rf-draft-preview')).toContainText('Right SMA · WSUB3G');
  await page.getByRole('button', { name: 'Open original sweep' }).click();
  const original = page.getByRole('dialog', { name: 'Original sweep' });
  await expect(original.getByRole('row', { name: '1 80010000 -42' })).toHaveAttribute(
    'aria-current',
    'true',
  );
  await expect(original.getByText('Reference tolerance envelope', { exact: true })).toHaveCount(0);
  await expect(original.getByText('Single-window band power', { exact: true })).toHaveCount(0);
  expect(reads).toContain('/api/v1/sessions/' + session.id + '/sweeps/' + source.id);
  expect(denied).toEqual([]);
});
