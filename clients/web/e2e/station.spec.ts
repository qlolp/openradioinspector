import { expect, test } from '@playwright/test';
import { openDesk } from './helpers';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
});

test('selected event exposes its own AR observation, UTC and original panorama in two actions', async ({
  page,
  request,
}) => {
  await openDesk(page);
  await page.getByTestId('project-select').selectOption({ label: 'North laboratory' });
  const project = await page.getByTestId('project-select').inputValue();
  const api = 'http://127.0.0.1:18772/api/v1/monitoring-projects/' + project;
  const events = (await (await request.get(api + '/events?limit=100')).json()).data.items;
  const observed =
    events.find(
      (item: { analysis?: { receiver_status?: unknown[] } }) =>
        item.analysis?.receiver_status?.length,
    ) ?? events[0];
  const event = (await (await request.get(api + '/events/' + observed.id)).json()).data;
  const observation = event.analysis.receiver_status.at(-1);
  const source =
    event.source_refs?.at(-1) ??
    (await (await request.get(api + '/events/' + event.id + '/sources?limit=1')).json()).data
      .items[0];
  const row = page.locator('[data-event-id="' + event.id + '"]');
  const details = row.getByRole('button', { name: 'Details' });
  await details.focus();
  await page.keyboard.press('Enter');
  const panel = page.getByRole('dialog', { name: 'Event details' });
  const evidence = panel.getByRole('region', { name: 'Event receiver observations' });
  await expect(evidence).toContainText(observation.id);
  await expect(evidence).toContainText(observation.level + ' S-meter counts');
  await expect(evidence).toContainText('UTC');
  await panel.getByRole('button', { name: 'Original sweep' }).click();
  const original = page.getByRole('dialog', { name: 'Original sweep' });
  await expect(original.getByRole('heading', { name: 'Source panorama' })).toBeVisible();
  await expect(original.locator('canvas.spectrum')).toHaveAttribute(
    'data-sweep-id',
    source.sweep_id,
  );
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await expect(details).toBeFocused();
  await expect(row).toHaveAttribute('data-selected', 'true');
  await page.getByTestId('project-select').selectOption({ label: 'Unprepared project' });
  await expect(page.getByTestId('event-row')).toHaveCount(0);
});

test('reported archive budget, writer admission and a USB fault give concrete recovery without side effects', async ({
  page,
}) => {
  await page.route('**/monitoring-projects/*/storage', (route) =>
    route.fulfill({
      json: {
        contract: 'openradioinspector-platform',
        contract_version: 1,
        data: {
          ready: false,
          capacity: { state: 'full' },
          filesystem_free_bytes: 1048576,
          estimated_retention_seconds: 5400,
          mean_stored_sweep_bytes: 512,
          projected_bytes_per_day: 2097152,
          wal_bytes: 0,
          backup_reserve_bytes: 1024,
          writer_queue: {
            queued_operations: 2,
            writer_capacity: 16,
            retained_bytes: 100,
            writer_byte_limit: 2000,
          },
        },
      },
    }),
  );
  await page.route('**/monitoring-projects/*/status', async (route) => {
    const response = await route.fetch(),
      body = await response.json();
    const data = body.data;
    data.workers = [
      {
        stream_id: data.project.receiver_stream_id,
        device: 'AR-DV1',
        adapter: 'ardv1-serial',
        state: 'error',
        alive: false,
        last_error: 'USB disconnected',
        measurements: 0,
        current: {},
      },
    ];
    await route.fulfill({ response, json: body });
  });
  const writes: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST') writes.push(r.url());
  });
  await openDesk(page);
  await page.getByTestId('project-select').selectOption({ label: 'North laboratory' });
  await expect(page.getByText('Archive needs attention', { exact: true })).toBeVisible();
  await expect(page.getByText('Receiver needs attention', { exact: true })).toBeVisible();
  const status = page.locator('.station-status');
  await expect(status).toContainText('2 / 16');
  await expect(status).toContainText('2 MiB');
  await expect(status).toContainText('1.5 hours');
  await status.getByText('Storage and measurement details').click();
  await expect(status).toContainText('512 B');
  await page.getByRole('button', { name: 'Recheck status' }).click();
  await page.keyboard.press('Alt+r');
  await expect(page.getByRole('button', { name: 'Research', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  expect(writes).toEqual([]);
  await page.unrouteAll({ behavior: 'wait' });
});

test('an advertised 10 kHz grid submits inclusive points and the selected RBW', async ({
  page,
}) => {
  await page.route('**/api/v1/capabilities', async (route) => {
    const response = await route.fetch(),
      body = await response.json();
    Object.assign(body.data.features, { monitoring_scan_plans: true, scan_plan_grid_10khz: true });
    await route.fulfill({ response, json: body });
  });
  await page.route('**/monitoring-projects/*/scan-plan', (route) =>
    route.fulfill({
      json: { contract: 'openradioinspector-platform', contract_version: 1, data: {} },
    }),
  );
  await openDesk(page);
  await page.getByTestId('project-select').selectOption({ label: 'North laboratory' });
  await page.getByLabel('Frequency grid').selectOption('10000');
  await page.getByLabel('Band start (Hz)').fill('100000');
  await page.getByLabel('Band end (Hz)').fill('1300000000');
  await page.getByLabel('Requested RBW').selectOption('30000');
  await expect(page.getByLabel('Logical points')).toHaveValue('129991');
  const submitted = page.waitForRequest(
    (r) => r.method() === 'POST' && r.url().endsWith('/scan-plan'),
  );
  await page.getByRole('button', { name: 'Save plan revision' }).click();
  expect((await submitted).postDataJSON().bands[0]).toMatchObject({
    start_hz: 100000,
    stop_hz: 1300000000,
    points: 129991,
    step_hz: 10000,
    rbw_hz: 30000,
  });
});

for (const [name, width, height, scale] of [
  ['Pi', 1366, 768, 1],
  ['Windows 125%', 1536, 864, 1.25],
  ['Windows 150%', 1280, 720, 1.5],
  ['Windows 175%', 1097, 617, 1.75],
  ['Windows 200%', 960, 540, 2],
] as const) {
  test.describe(name, () => {
    test.use({ viewport: { width, height }, deviceScaleFactor: scale });
    test('fits controls and keeps event selection beside the live instruments', async ({
      page,
    }) => {
      const errors: string[] = [];
      page.on('pageerror', (error) => errors.push(error.message));
      await openDesk(page);
      await page.getByTestId('project-select').selectOption({ label: 'North laboratory' });
      const details = page
        .getByTestId('event-row')
        .first()
        .getByRole('button', { name: 'Details' });
      const plot = page.locator('.spectrum-panel .spectrum-plot');
      await expect(details).toBeVisible();
      const box = await details.boundingBox(),
        graph = await plot.boundingBox();
      expect(box!.x).toBeGreaterThan(graph!.x + graph!.width);
      expect(box!.y).toBeLessThan(height);
      if (height >= 768) {
        const waterfall = await page.locator('.waterfall').boundingBox();
        expect(Math.min(waterfall!.height, height - waterfall!.y)).toBeGreaterThanOrEqual(110);
      }
      const writes: string[] = [];
      page.on('request', (r) => {
        if (r.method() === 'POST') writes.push(r.url());
      });
      for (const theme of ['dark', 'light', 'system']) {
        await page.getByRole('combobox', { name: 'Theme', exact: true }).selectOption(theme);
        await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
        true,
      );
      await details.focus();
      await page.keyboard.press('Shift+Tab');
      await page.keyboard.press('Tab');
      await expect(details).toBeFocused();
      expect(await details.evaluate((b) => getComputedStyle(b).outlineStyle)).toBe('solid');
      expect(writes).toEqual([]);
      expect(errors).toEqual([]);
    });
  });
}

test('a saved bounded receiver session starts and stops only its own receiver and survives reload', async ({
  page,
  request,
}) => {
  const endpoint = 'http://127.0.0.1:18772/api/v1';
  const projects = (await (await request.get(endpoint + '/monitoring-projects')).json()).data.items;
  const project = projects.find((item: { name: string }) => item.name === 'North laboratory');
  await page.addInitScript((id) => localStorage.setItem('ori-modern-project', id), project.id);
  const status = await (
    await request.get(endpoint + '/monitoring-projects/' + project.id + '/status')
  ).json();
  status.data.active = true;
  status.data.project.state = 'armed';
  status.data.project.profile.receiver.settings.adapter = 'ardv1-serial';
  status.data.project.profile.receiver.port = 'fixture-port';
  await page.route('**/monitoring-projects/' + project.id + '/status', (route) =>
    route.fulfill({ json: status }),
  );
  const caps = await (await request.get(endpoint + '/capabilities')).json();
  Object.assign(caps.data.features, { ardv1_sampled_scope: true, graceful_device_stop: true });
  await page.route('**/api/v1/capabilities', (route) => route.fulfill({ json: caps }));
  const baseSessions = (await (await request.get(endpoint + '/sessions')).json()).data;
  const sid = 'fixture-receiver-segment',
    stream = 'fixture-stream';
  const envelope = (data: unknown) => ({
    contract: 'openradioinspector-platform',
    contract_version: 1,
    data,
  });
  let saved: any,
    alive = false,
    count = 0;
  const calls: string[] = [];
  await page.route('**/api/v1/sessions', (route) => {
    if (route.request().method() === 'POST') {
      const input = route.request().postDataJSON();
      expect(input.streams.length).toBe(1);
      expect(input.streams[0].settings.fast_scan).toMatchObject({
        start_hz: 100000000,
        stop_hz: 100100000,
        step_hz: 25000,
      });
      saved = {
        ...baseSessions[0],
        ...input,
        id: sid,
        state: 'draft',
        streams: [
          { ...baseSessions[0].streams[1], ...input.streams[0], id: stream, session_id: sid },
        ],
      };
      calls.push('prepare');
      return route.fulfill({ json: envelope(saved) });
    }
    return route.fulfill({ json: envelope(saved ? [...baseSessions, saved] : baseSessions) });
  });
  await page.route('**/api/v1/sessions/' + sid + '/**', (route) => {
    const url = new URL(route.request().url()).pathname.split(sid + '/')[1];
    if (route.request().method() === 'POST') {
      calls.push(url);
      if (url === 'start') saved.state = 'running';
      else if (url === 'streams/' + stream + '/start') {
        alive = true;
        count = 2;
      } else if (url === 'streams/' + stream + '/stop') {
        expect(route.request().postDataJSON()).toEqual({ mode: 'graceful' });
        alive = false;
      } else if (url === 'stop') saved.state = 'stopped';
      else throw Error('Unexpected receiver session mutation ' + url);
      return route.fulfill({ json: envelope({}) });
    }
    return route.fulfill({
      json: envelope(
        url === 'workers'
          ? [
              {
                stream_id: stream,
                state: alive ? 'running' : 'stopped',
                alive,
                measurements: count,
                current: {},
              },
            ]
          : [],
      ),
    });
  });
  await openDesk(page);
  await page.getByRole('button', { name: 'Research', exact: true }).click();
  await page.getByRole('button', { name: 'Prepare receiver segment', exact: true }).click();
  await expect(
    page.getByText('The monitoring project owns the receiver while collection or guard is active.'),
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Start receiver segment', exact: true }),
  ).toBeDisabled();
  status.data.active = false;
  status.data.project.state = 'ready';
  await expect(
    page.getByRole('button', { name: 'Start receiver segment', exact: true }),
  ).toBeEnabled();
  expect(calls).toEqual(['prepare']);
  await page.getByRole('button', { name: 'Start receiver segment', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Stop receiver segment', exact: true }),
  ).toBeEnabled();
  await page.reload();
  await page.getByTestId('legacy-compatibility-consent').check();
  await page.getByTestId('legacy-compatibility-continue').click();
  await page.getByRole('button', { name: 'Stop receiver segment', exact: true }).click();
  await page.getByRole('button', { name: 'Finish segment session', exact: true }).click();
  await expect.poll(() => calls.at(-1)).toBe('stop');
  expect(calls).toEqual([
    'prepare',
    'start',
    'streams/' + stream + '/start',
    'streams/' + stream + '/stop',
    'stop',
  ]);
  await page.unrouteAll({ behavior: 'wait' });
});

test('gridded source keeps its measured window separate from whole-band coverage', async ({
  page,
  request,
}) => {
  const api = 'http://127.0.0.1:18772/api/v1';
  const list = (await (await request.get(api + '/monitoring-projects')).json()).data.items;
  const project = list.find((item: { name: string }) => item.name === 'North laboratory'),
    path = '/monitoring-projects/' + project.id;
  await page.addInitScript((id) => localStorage.setItem('ori-modern-project', id), project.id);
  const status = await (await request.get(api + path + '/status')).json();
  const band = {
    id: 'wide',
    name: 'Fixture band',
    step_hz: 10000,
    start_hz: 88000000,
    stop_hz: 108000000,
    points: 2001,
    rbw_hz: 30000,
    max_revisit_seconds: 180,
  };
  status.data.project.scan_plan = {
    id: 'fixture-plan',
    revision: 1,
    config: { name: 'Fixture plan', bands: [band] },
  };
  const sweepPath = '/sessions/' + status.data.project.active_session_id + '/sweeps';
  const sampled = await (
    await request.get(
      api + sweepPath + '?stream_id=' + status.data.project.analyzer_stream_id + '&limit=1',
    )
  ).json();
  const source = sampled.data[0];
  Object.assign(source, {
    id: 'fixture-window',
    frequencies_hz: Array.from({ length: 290 }, (_, i) => 88000000 + i * 10000),
    levels: Array.from({ length: 290 }, (_, i) => (i === 12 ? -30 : -95)),
    original_point_count: 290,
  });
  Object.assign(source.settings, {
    scan_plan_id: 'fixture-plan',
    scan_plan_revision: 1,
    scan_band_id: 'wide',
    scan_window_index: 0,
    scan_window_count: 7,
    scan_step_hz: 10000,
    start_hz: 88000000,
    stop_hz: 90890000,
    points: 290,
  });
  const last = {
    index: 0,
    start_hz: 88000000,
    stop_hz: 90890000,
    points: 290,
    measurement_id: source.id,
    started_at: source.interval.started_at,
    ended_at: source.interval.ended_at,
  };
  status.data.workers = [
    {
      stream_id: status.data.project.analyzer_stream_id,
      device: 'tinySA',
      adapter: 'tinysa-sim',
      state: 'running',
      alive: true,
      measurements: 1,
      current: {
        scan_coverage: {
          bands: [
            {
              id: 'wide',
              name: 'Fixture band',
              state: 'partial',
              age_seconds: 5,
              last_revisit_seconds: null,
              deadline_missed: false,
              observed_windows: 1,
              window_count: 7,
              completed_passes: 0,
              last_window: last,
            },
          ],
        },
      },
    },
  ];
  await page.route('**' + path + '/status', (route) => route.fulfill({ json: status }));
  await page.route('**' + sweepPath + '?*', (route) => route.fulfill({ json: sampled }));
  await openDesk(page);
  const panel = page.locator('.spectrum-panel');
  await expect(panel.getByText('Band panorama', { exact: true })).toBeVisible();
  await expect(panel).toContainText('Observed windows 1 / 7');
  await expect(panel).toContainText('Completed passes 0');
  await expect(panel.getByRole('heading', { name: 'Latest hardware window' })).toBeVisible();
  await expect(panel.locator('canvas.spectrum')).toHaveAttribute('data-sweep-id', 'fixture-window');
  await expect(panel.locator('.waterfall-title')).toContainText('108,000,000 Hz');
  await expect
    .poll(() =>
      panel.locator('.band-overview canvas').evaluate((element: HTMLCanvasElement) => {
        const pixels = element.getContext('2d')!.getImageData(0, 0, element.width, element.height);
        let filled = 0,
          empty = 0;
        for (let x = 0; x < pixels.width; x++) {
          if (pixels.data[((pixels.height - 1) * pixels.width + x) * 4 + 3]) filled++;
          else empty++;
        }
        return filled > 0 && empty > filled;
      }),
    )
    .toBe(true);
  await page.unrouteAll({ behavior: 'wait' });
});
