import { expect, test, type Page } from '@playwright/test';
import { openDesk } from './helpers';

const envelope = (data: unknown) => ({
  contract: 'openradioinspector-platform',
  contract_version: 1,
  data,
});
const started = '2026-11-20T00:00:00.000000Z',
  ended = '2026-11-20T00:01:02.000000Z';
const sid = 'duty-synthetic',
  stream = 'duty-receiver',
  pid = 'synthetic-project';
const raw = {
  id: 'synthetic-original',
  session_id: sid,
  stream_id: stream,
  frequency_hz: 100020000,
  mode: 'FM',
  level_unit: 's_meter_count',
  level: 13,
  metadata: { frequency_source: 'rx_readback', simulated: true },
  interval: { started_at: started, ended_at: ended },
};
const project = {
  id: pid,
  name: 'Synthetic AR-DV1 archive',
  site: 'Synthetic browser fixture',
  state: 'ready',
  primary_source_kind: 'ardv1',
  primary_stream_id: stream,
  active_session_id: sid,
  receiver_stream_id: stream,
  analyzer_stream_id: null,
  coverage: 'stopped',
  baseline_id: 'background',
  preparation: { antenna_notes: 'Fixture', receive_path_confirmed: true, location_confirmed: true },
};
const pass = {
  pass: 2,
  start_hz: 100000000,
  stop_hz: 100080000,
  step_hz: 10000,
  total_points: 9,
  measured_points: 7,
  complete: false,
  level_unit: 's_meter_count',
  lower_is_stronger: true,
  started_at: started,
  ended_at: ended,
  coverage_hex: 'cf01',
  bins: [0, 1, 2, 3, 6, 7, 8].map((index, i) => ({
    start_index: index,
    end_index: index,
    measured_points: 1,
    frequency_hz: 100000000 + index * 10000,
    level: [153, 109, 13, 146, 131, 101, 142][i],
    observation_id: index === 2 ? raw.id : 'sample-' + index,
    started_at: started,
    ended_at: ended,
  })),
};

async function capabilities(page: Page) {
  await page.route('**/api/v1/capabilities', async (route) => {
    const response = await route.fetch(),
      data = (await response.json()).data;
    Object.assign(data.features, {
      single_source_duty: true,
      ardv1_cyclic_panorama: true,
      ardv1_receiver_panorama: true,
      monitoring_signal_catalog: true,
      explicit_serial_adapters: true,
      archive_location: true,
      serial_port_candidates: true,
      monitoring_draft_port_correction: true,
      rf_explorer_3g_spectrum: true,
      rf_explorer_10khz_survey: true,
      scan_plan_grid_10khz: true,
    });
    await route.fulfill({ response, json: envelope(data) });
  });
}
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
  await capabilities(page);
});

test('operator can select a receiver alone and validate a wide inclusive plan before any connection', async ({
  page,
}) => {
  const posts: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST') posts.push(new URL(r.url()).pathname);
  });
  await page.route('**/api/v1/serial-ports', (route) =>
    route.fulfill({
      json: envelope({ ports: ['COM27', 'COM28'] }),
    }),
  );
  await openDesk(page);
  await page.getByRole('button', { name: 'Prepare project' }).click();
  const wizard = page.getByRole('dialog', { name: 'Prepare a receive-only project' });
  await wizard.getByLabel('Project name').fill('Synthetic duty preparation');
  await wizard.getByLabel('Site or location').fill('Fixture');
  await wizard.getByLabel('Primary instrument').selectOption('ardv1', { timeout: 2500 });
  await expect(wizard.locator('datalist option[value="COM27"]')).toHaveCount(1);
  await expect(wizard.getByLabel('AR-DV1 serial port')).toHaveAttribute(
    'list',
    'duty-serial-ports',
  );
  await wizard.getByLabel('AR-DV1 serial port').fill('COM28');
  await wizard.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(wizard.getByLabel('Start (MHz)')).toHaveValue('100');
  await expect(wizard.getByLabel('Stop (MHz)')).toHaveValue('100.2');
  await expect(wizard.getByText('21', { exact: true })).toBeVisible();
  await wizard.getByRole('button', { name: '100–400 MHz' }).click();
  await expect(wizard.getByText('30,001', { exact: true })).toBeVisible();
  await wizard.getByRole('button', { name: 'Full supported range' }).click();
  await expect(wizard.getByText('129,991', { exact: true })).toBeVisible();
  await wizard.getByLabel('Step (kHz)').fill('1');
  await expect(wizard.getByRole('alert')).toContainText('Increase the step');
  await expect(wizard.getByRole('button', { name: 'Next', exact: true })).toBeDisabled();
  await wizard.getByLabel('Step (kHz)').fill('10');
  await wizard.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(
    wizard.getByRole('heading', { name: 'Archive and first measurement' }),
  ).toBeVisible();
  expect(posts).toEqual([]);
});

test('a stopped project can correct a port without creating another project or starting a worker', async ({
  page,
}) => {
  await page.addInitScript((id) => localStorage.setItem('ori-modern-project', id), pid);
  const writes: string[] = [];
  let port = 'COM14';
  const current = () => ({
    ...project,
    state: 'draft',
    baseline_id: null,
    profile: {
      receiver: { id: stream, role: 'receiver', port, settings: { adapter: 'ardv1-serial' } },
    },
  });
  await page.route('**/api/v1/serial-ports', (route) =>
    route.fulfill({ json: envelope({ ports: ['COM24'] }) }),
  );
  await page.route('**/api/v1/monitoring-projects?*', (route) =>
    route.fulfill({ json: envelope({ items: [current()], next_cursor: null }) }),
  );
  await page.route('**/api/v1/monitoring-projects/' + pid + '/**', async (route) => {
    const tail = new URL(route.request().url()).pathname.split(pid + '/')[1];
    if (route.request().method() === 'POST') {
      writes.push(tail);
      expect(tail).toBe('preparation');
      expect(route.request().postDataJSON().port_corrections).toEqual({
        receiver: { from: 'COM14', to: 'COM24' },
      });
      port = 'COM24';
      return route.fulfill({ json: envelope(current()) });
    }
    const value =
      tail === 'status'
        ? { project: current(), workers: [], manual: [], active: false }
        : tail === 'preflight'
          ? { checks: [], baseline_ready: false, baseline_collection: { ready: false } }
          : { items: [], next_cursor: null };
    return route.fulfill({ json: envelope(value) });
  });
  await openDesk(page);
  await page.getByRole('button', { name: 'Duty setup' }).click();
  const wizard = page.getByRole('dialog', { name: 'Prepare a receive-only project' });
  await wizard.getByText('Correct the project ports').click();
  await expect(wizard.getByLabel('AR-DV1 serial port')).toHaveValue('COM14');
  await wizard.getByLabel('AR-DV1 serial port').fill('COM24');
  await wizard.getByRole('button', { name: 'Save project ports' }).click();
  await expect(wizard.getByRole('button', { name: 'Save project ports' })).toBeDisabled();
  await expect(wizard.getByLabel('AR-DV1 serial port')).toHaveValue('COM24');
  expect(writes).toEqual(['preparation']);
  await wizard.getByRole('button', { name: 'Change instruments or range in a new draft' }).click();
  await expect(wizard.getByRole('heading', { name: 'Choose instruments' })).toBeVisible();
  await expect(wizard.getByLabel('AR-DV1 serial port')).toHaveValue('COM24');
});
test('receiver duty opens measured-only pass, waterfall and original evidence in the monitoring workspace', async ({
  page,
}, testInfo) => {
  await page.addInitScript((id) => localStorage.setItem('ori-modern-project', id), pid);
  await page.route('**/api/v1/monitoring-projects?*', (r) =>
    r.fulfill({ json: envelope({ items: [project], next_cursor: null }) }),
  );
  await page.route('**/api/v1/monitoring-projects/' + pid + '/**', async (r) => {
    const path = new URL(r.request().url()).pathname.split(pid + '/')[1];
    const data =
      path === 'status'
        ? {
            project,
            active: false,
            receiver_busy: false,
            manual: [],
            workers: [],
            hardware_validation: 'not_run',
          }
        : path === 'preflight'
          ? {
              ready: true,
              baseline_ready: true,
              checks: [],
              baseline_collection: {
                points: 9,
                ready_points: 9,
                passes: 20,
                minimum_passes: 20,
                minimum_seconds: 60,
                elapsed_seconds: 62,
                ready: true,
              },
            }
          : path === 'search/status'
            ? { active: false, marks: [], series: [] }
            : path === 'storage'
              ? { ready: true }
              : { items: [], next_cursor: null };
    return r.fulfill({ json: envelope(data) });
  });
  const receiverBase = '**/api/v1/sessions/' + sid + '/streams/' + stream + '/receiver-panorama';
  await page.route(receiverBase + '?*', (route) => {
    const number = Number(new URL(route.request().url()).searchParams.get('pass') || 6);
    const offset = (6 - number) * 63_000;
    const since = new Date(Date.parse(started) - offset).toISOString(),
      until = new Date(Date.parse(ended) - offset).toISOString();
    return route.fulfill({
      json: envelope({
        ...pass,
        pass: number,
        started_at: since,
        ended_at: until,
        bins: pass.bins.map((b, i) => ({
          ...b,
          started_at: since,
          ended_at: until,
          observation_id: number === 6 ? b.observation_id : 'synthetic-pass-' + number + '-' + i,
          level:
            number === 6
              ? b.level
              : Math.max(1, b.level + ((number % 3) - 1) * 11 * (i % 2 === 0 ? 1 : -1)),
        })),
      }),
    });
  });
  await page.route(receiverBase + '/point?*', (r) => r.fulfill({ json: envelope(raw) }));
  await page.route('**/api/v1/sessions/' + sid + '/observations?*', (r) =>
    r.fulfill({ json: envelope([raw]) }),
  );
  await page.route('**/api/v1/sessions/' + sid + '/observations/' + raw.id, (r) =>
    r.fulfill({ json: envelope(raw) }),
  );
  await openDesk(page);
  const panel = page.getByRole('region', { name: 'Receiver panorama' });
  await expect(panel).toBeVisible({ timeout: 3500 });
  await expect(panel).toContainText('7 / 9');
  await expect(panel).toContainText(/2 unmeasured/i);
  await expect(panel.getByRole('img', { name: 'Waterfall by pass' })).toBeVisible();
  await expect(panel).not.toContainText('dBm');
  await panel.getByRole('button', { name: 'Strongest original' }).click();
  await expect(page.getByRole('dialog', { name: 'Original observation' })).toContainText(raw.id);
  await page.getByRole('button', { name: 'Close original' }).click();
  await expect(panel).toContainText('6 / 5 / 4 / 3 / 2 / 1');
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('synthetic-receiver-duty.png') });
  await page.getByTestId('locale-select').selectOption('ru');
  await page.screenshot({ path: testInfo.outputPath('synthetic-receiver-duty-ru.png') });
  await page.getByTestId('locale-select').selectOption('en');
  await panel.getByRole('button', { name: 'Previous pass' }).click();
  await expect(panel.getByText('Pass 5', { exact: true })).toBeVisible();
});

test('receiver event opens its exact observation and hold/resume uses an explicit receiver lease', async ({
  page,
}) => {
  await page.addInitScript((id) => localStorage.setItem('ori-modern-project', id), pid);
  const writes: { path: string; body: unknown }[] = [];
  let held = false;
  const single = { ...project, state: 'armed', coverage: 'wide' };
  const event = {
    id: 'synthetic-event',
    project_id: pid,
    run_session_id: sid,
    kind: 'new_signal',
    state: 'open',
    first_seen: started,
    last_seen: ended,
    center_frequency_hz: raw.frequency_hz,
    start_frequency_hz: raw.frequency_hz,
    end_frequency_hz: raw.frequency_hz,
    peak_level: 13,
    level_unit: 's_meter_count',
    source_count: 1,
    disposition: 'unreviewed',
    peak_source: {
      session_id: sid,
      stream_id: stream,
      observation_id: raw.id,
      frequency_hz: raw.frequency_hz,
    },
    source_refs: [{ session_id: sid, stream_id: stream, observation_id: raw.id }],
  };
  await page.route('**/api/v1/monitoring-projects?*', (r) =>
    r.fulfill({ json: envelope({ items: [single], next_cursor: null }) }),
  );
  await page.route('**/api/v1/monitoring-projects/' + pid + '/**', async (r) => {
    const path = new URL(r.request().url()).pathname.split(pid + '/')[1];
    if (r.request().method() === 'POST') {
      writes.push({ path, body: r.request().postDataJSON() });
      if (path === 'manual/acquire') {
        held = true;
        return r.fulfill({ json: envelope({ token: 'test-lease', expires_seconds: 30 }) });
      }
      if (path === 'manual/return') held = false;
      return r.fulfill({ json: envelope({ state: 'ok' }) });
    }
    const data =
      path === 'status'
        ? {
            project: { ...single, coverage: held ? 'paused' : 'wide' },
            active: true,
            receiver_busy: false,
            manual: held ? [{ role: 'receiver', expired: false, expires_seconds: 30 }] : [],
            workers: [],
            hardware_validation: 'not_run',
          }
        : path === 'preflight'
          ? {
              ready: true,
              baseline_ready: true,
              checks: [],
              baseline_collection: {
                points: 9,
                ready_points: 9,
                passes: 20,
                ready: true,
                elapsed_seconds: 62,
              },
            }
          : path === 'search/status'
            ? { active: false, marks: [], series: [] }
            : path === 'events'
              ? { items: [event], next_cursor: null }
              : path === 'events/' + event.id
                ? event
                : path === 'events/' + event.id + '/recognition'
                  ? {
                      ...event,
                      recognition: {
                        state: 'new',
                        hypotheses: [
                          {
                            label: 'Synthetic broadcast service',
                            reason: 'Regional frequency-band match',
                          },
                        ],
                        known_matches: [],
                        band_matches: [],
                      },
                    }
                  : { items: [], next_cursor: null };
    return r.fulfill({ json: envelope(data) });
  });
  await page.route(
    '**/api/v1/sessions/' + sid + '/streams/' + stream + '/receiver-panorama?*',
    (r) => r.fulfill({ json: envelope({ ...pass, pass: 1 }) }),
  );
  await page.route('**/api/v1/sessions/' + sid + '/observations?*', (r) =>
    r.fulfill({ json: envelope([raw]) }),
  );
  await page.route('**/api/v1/sessions/' + sid + '/observations/' + raw.id, (r) =>
    r.fulfill({ json: envelope(raw) }),
  );
  await openDesk(page);
  await page.getByTestId('event-row').getByRole('button', { name: 'Details' }).click();
  const dialog = page.getByRole('dialog', { name: 'Event details' });
  await expect(dialog).not.toContainText('dBm');
  await expect(dialog.getByRole('region', { name: 'Signal hypotheses' })).toContainText(
    'Synthetic broadcast service',
  );
  await expect(dialog.getByLabel('Disposition')).toHaveValue('unreviewed');
  await dialog.getByRole('button', { name: 'Original observation' }).click();
  await expect(page.getByRole('dialog', { name: 'Original observation' })).toContainText(raw.id);
  await page.getByRole('button', { name: 'Close original' }).click();
  await dialog.getByRole('button', { name: 'Close event' }).click();
  await page.getByLabel('Hold frequency (Hz)').fill('100020000');
  await page.getByRole('button', { name: 'Hold frequency and pause panorama' }).click();
  await expect(page.getByRole('button', { name: 'Resume broad coverage' })).toBeEnabled();
  await page.getByRole('button', { name: 'Resume broad coverage' }).click();
  await expect
    .poll(() => writes)
    .toEqual([
      { path: 'manual/acquire', body: { role: 'receiver' } },
      { path: 'manual/tune', body: { token: 'test-lease', frequency_hz: 100020000, mode: 'FM' } },
      { path: 'manual/return', body: { token: 'test-lease' } },
    ]);
});

test('five steps require an archived sample and confirmed baseline before scheduling with notification channels off', async ({
  page,
}) => {
  test.setTimeout(60_000);
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  let created = false,
    collecting = false,
    baseline = false,
    sampled = false,
    progress = false,
    scheduled = false;
  let webhookEnabled = false;
  let webhookConfigured = false;
  let testReceipt = false;
  let delivered = false;
  let allowSchedule: () => void;
  const scheduleRead = new Promise<void>((resolve) => {
    allowSchedule = resolve;
  });
  const running = { ...project, baseline_id: null, state: 'draft' };
  await page.route('**/api/v1/capabilities', async (r) => {
    const res = await r.fetch(),
      data = (await res.json()).data;
    Object.assign(data.features, {
      single_source_duty: true,
      ardv1_cyclic_panorama: true,
      ardv1_receiver_panorama: true,
      archive_location: true,
      monitoring_duty_schedule: true,
      monitoring_notification_channels: true,
      monitoring_storage_forecast: true,
    });
    await r.fulfill({ json: envelope(data) });
  });
  await page.route('**/api/v1/archive/location', (r) =>
    r.fulfill({
      json: envelope({
        storage_id: 'node-archive',
        label: 'Synthetic SSD',
        database: 'sessions.sqlite3',
        available_bytes: 8589934592,
        relocation: 'local-supervisor',
      }),
    }),
  );
  await page.route('**/api/v1/sessions', async (r) => {
    if (r.request().method() !== 'POST') return r.continue();
    writes.push({ path: 'sessions', body: r.request().postDataJSON() });
    await r.fulfill({ json: envelope({ id: sid, name: 'Synthetic duty', streams: [] }) });
  });
  await page.route('**/api/v1/monitoring-projects', async (r) => {
    if (r.request().method() !== 'POST') return r.continue();
    created = true;
    writes.push({ path: 'monitoring-projects', body: r.request().postDataJSON() });
    await r.fulfill({ json: envelope(running) });
  });
  await page.route('**/api/v1/monitoring-projects?*', (r) =>
    r.fulfill({ json: envelope({ items: created ? [running] : [], next_cursor: null }) }),
  );
  await page.route('**/api/v1/monitoring-projects/' + pid + '/**', async (r) => {
    const path = new URL(r.request().url()).pathname.split(pid + '/')[1];
    if (r.request().method() === 'POST') {
      writes.push({ path, body: r.request().postDataJSON() });
      if (path === 'preparation/start') collecting = true;
      if (path === 'baselines') baseline = true;
      if (path === 'duty') scheduled = true;
      if (path === 'settings' && r.request().postDataJSON().notifications) {
        const update = r.request().postDataJSON().notifications.webhook;
        webhookEnabled = update.enabled;
        if (update.url) webhookConfigured = true;
      }
      if (path === 'notifications/test') {
        testReceipt = true;
        return r.fulfill({
          json: envelope({
            notification_id: 'synthetic-queued-test',
            channel: 'webhook',
            state: 'pending',
          }),
        });
      }
      return r.fulfill({
        json: envelope(
          path === 'duty'
            ? { enabled: true, status: 'waiting' }
            : path === 'preparation/start'
              ? { project: { ...running, state: 'collecting' } }
              : running,
        ),
      });
    }
    if (path === 'duty') await scheduleRead;
    const state = {
      ...running,
      state: baseline ? 'ready' : collecting ? 'collecting' : 'draft',
      ...(scheduled && { duty: { enabled: true, status: 'waiting' } }),
      baseline_id: baseline ? 'confirmed' : null,
    };
    const status = {
      project: state,
      active: false,
      hardware_validation: 'not_run',
      manual: [],
      workers: [],
      receiver_busy: false,
    };
    const data =
      path === 'status'
        ? status
        : path === 'preflight'
          ? {
              ready: true,
              baseline_ready: baseline,
              checks: [],
              baseline_collection: {
                points: 9,
                ready_points: progress ? 9 : 0,
                passes: progress ? 20 : 1,
                minimum_passes: 20,
                minimum_seconds: 60,
                elapsed_seconds: progress ? 62 : 0,
                ready: progress,
              },
            }
          : path === 'search/status'
            ? { active: false, marks: [], series: [] }
            : path === 'duty'
              ? {
                  enabled: scheduled,
                  status: scheduled ? 'waiting' : 'off',
                  schedule: { mode: 'continuous' },
                  resume_after_restart: false,
                  deliveries: testReceipt
                    ? [
                        {
                          id: 'synthetic-queued-test',
                          channel: 'webhook',
                          kind: 'test',
                          state: delivered ? 'delivered' : 'pending',
                          attempts: delivered ? 1 : 0,
                          created_at: started,
                        },
                      ]
                    : [],
                }
              : path === 'settings'
                ? {
                    notifications: {
                      telegram: { enabled: false, configured: false },
                      webhook: { enabled: webhookEnabled, configured: webhookConfigured },
                    },
                  }
                : path === 'storage'
                  ? {
                      ready: true,
                      storage_id: 'node-archive',
                      forecast_seconds: 36_000,
                      required_free_bytes: 5 * 1024 ** 3,
                      available_bytes: 8 * 1024 ** 3,
                      shortfall_bytes: 0,
                      projected_bytes_per_day: 8640000,
                    }
                  : { items: [], next_cursor: null };
    await r.fulfill({ json: envelope(data) });
  });
  await page.route('**/api/v1/sessions/' + sid + '/observations/' + raw.id, (r) =>
    r.fulfill({ json: envelope(raw) }),
  );
  await page.route('**/api/v1/sessions/' + sid + '/observations?*', (r) =>
    r.fulfill({ json: envelope(sampled ? [raw] : []) }),
  );
  await page.route('**/receiver-panorama?*', (r) =>
    r.fulfill({ json: envelope({ ...pass, pass: 1 }) }),
  );
  await openDesk(page);
  await page.getByRole('button', { name: 'Prepare project' }).click();
  const wizard = page.getByRole('dialog', { name: 'Prepare a receive-only project' });
  await wizard.getByLabel('Project name').fill('Synthetic duty');
  await wizard.getByLabel('Site or location').fill('Synthetic test bench');
  await wizard.getByLabel('Primary instrument').selectOption('ardv1');
  await wizard.getByLabel('AR-DV1 serial port').fill('SYNTHETIC-NO-OPEN');
  await wizard.getByRole('button', { name: 'Next', exact: true }).click();
  await wizard.getByRole('button', { name: 'Next', exact: true }).click();
  await wizard.getByLabel('Archive on the recording node').selectOption('node-archive');
  await wizard.getByLabel('Antenna and receive path notes').fill('Synthetic fixture');
  await wizard.getByLabel('Receive path and antenna checked').check();
  await wizard.getByLabel('Location and ordinary RF environment checked').check();
  expect(writes).toEqual([]);
  await wizard.getByRole('button', { name: 'Start receive collection' }).click();
  await expect.poll(() => collecting).toBe(true);
  await expect(wizard.getByRole('button', { name: 'Next', exact: true })).toHaveCount(0);
  await wizard.getByRole('button', { name: 'Close setup' }).click();
  await page.getByRole('button', { name: 'Duty setup' }).click();
  await expect(
    wizard.getByRole('heading', { name: 'Archive and first measurement' }),
  ).toBeVisible();
  await expect(wizard.getByLabel('Archive on the recording node')).toHaveValue('node-archive');
  await expect(wizard.getByRole('button', { name: 'Next', exact: true })).toHaveCount(0);
  sampled = true;
  await expect(wizard).toContainText(raw.id, { timeout: 7000 });
  const original = wizard.locator('details').filter({ hasText: 'Open archived original' });
  await expect(original).toContainText('100,020,000 Hz');
  await expect(original).toContainText('13 S-meter counts');
  await original.locator('summary').click();
  await expect(original).toContainText('/api/v1/sessions/' + sid + '/observations/' + raw.id);
  await expect(original.locator('pre')).toContainText(raw.interval.started_at);
  await expect(original.locator('pre')).toContainText(raw.id);
  await wizard.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(wizard.getByText(/Required free space/)).toContainText('5 GiB');
  await wizard.getByLabel('Minimum archive horizon (hours)').fill('10');
  await wizard.getByLabel('Reserve space for a restore check').uncheck();
  await wizard.getByLabel('I confirm this baseline represents the ordinary environment').check();
  await expect(wizard.getByRole('button', { name: 'Confirm baseline' })).toBeDisabled();
  progress = true;
  await expect(wizard.getByRole('button', { name: 'Confirm baseline' })).toBeEnabled({
    timeout: 12000,
  });
  await wizard.getByRole('button', { name: 'Confirm baseline' }).click();
  await expect(wizard.getByLabel('Duty schedule')).toBeDisabled();
  allowSchedule!();
  await wizard.getByLabel('Duty schedule').selectOption('weekly');
  await wizard.getByLabel('Time zone').fill('Europe/Moscow');
  await expect(
    wizard.getByLabel('Resume duty after a node restart and a successful preflight'),
  ).not.toBeChecked();
  await wizard.getByRole('button', { name: 'Arm duty' }).click();
  await expect(wizard).toHaveCount(0);
  await expect(page.getByTestId('guard-state')).toHaveText('Waiting for scheduled window');
  await expect(page.getByRole('button', { name: 'Stop duty' })).toBeEnabled();
  expect(writes.map((w) => w.path)).toEqual([
    'sessions',
    'monitoring-projects',
    'preparation',
    'preparation/start',
    'settings',
    'baselines',
    'duty',
  ]);
  expect(writes.find((write) => write.path === 'settings')?.body).toEqual({
    archive: { minimum_retention_seconds: 36000, reserve_backup: true, reserve_restore: false },
  });
  expect(writes[0].body.streams).toHaveLength(1);
  expect(writes.at(-1)?.body).toEqual({
    resume_after_restart: false,
    schedule: {
      mode: 'weekly',
      weekdays: [1, 2, 3, 4, 5],
      start_local: '08:00',
      end_local: '18:00',
      timezone: 'Europe/Moscow',
    },
  });
  // Only the explicit, routed webhook test queues this synthetic receipt.
  await page.getByRole('button', { name: 'Duty setup' }).click();
  await wizard.getByText('Webhook · Disabled', { exact: true }).click();
  await wizard.getByLabel('Enable Webhook for this project').check();
  await wizard.getByLabel('Webhook URL').fill('https://example.invalid/synthetic');
  await wizard.getByLabel('Webhook secret').fill('synthetic-local-fixture');
  const webhook = wizard.locator('details').filter({ hasText: 'Webhook URL' });
  await webhook.getByRole('button', { name: 'Save channel' }).click();
  await expect(webhook.getByText('Webhook · Enabled', { exact: true })).toBeVisible();
  expect(testReceipt).toBe(false);
  await webhook.getByRole('button', { name: 'Send test notification' }).click();
  await expect(webhook).toContainText('synthetic-queued-test');
  await wizard.getByText('Notification delivery history', { exact: true }).click();
  const delivery = wizard.getByRole('listitem').filter({ hasText: 'synthetic-queued-test' });
  await expect(delivery).toContainText('pending');
  delivered = true;
  await expect(delivery).toContainText('Delivered', { timeout: 10000, ignoreCase: true });
  await expect(webhook.getByRole('status')).toContainText('Delivered', { ignoreCase: true });
  expect(writes.slice(-2)).toEqual([
    {
      path: 'settings',
      body: {
        notifications: {
          webhook: {
            enabled: true,
            url: 'https://example.invalid/synthetic',
            secret: 'synthetic-local-fixture',
          },
        },
      },
    },
    { path: 'notifications/test', body: { channel: 'webhook' } },
  ]);
});

test('observation-only tinySA windows can collect originals while arming stays unavailable', async ({
  page,
}) => {
  const upperSession = 'upper-synthetic-session',
    upperStream = 'upper-tinysa-stream',
    upperProject = 'upper-tinysa-project';
  const upper = {
    ...project,
    id: upperProject,
    name: 'Synthetic upper band',
    active_session_id: upperSession,
    primary_source_kind: 'tinysa',
    primary_stream_id: upperStream,
    receiver_stream_id: null,
    analyzer_stream_id: upperStream,
    baseline_id: null,
    state: 'draft',
  };
  let created = false,
    collecting = false;
  const writes: string[] = [];
  const envelopePage = (items: unknown[]) => envelope({ items, next_cursor: null });
  await page.route('**/api/v1/archive/location', (r) =>
    r.fulfill({
      json: envelope({
        storage_id: 'node-archive',
        label: 'Synthetic archive',
        database: 'sessions.sqlite3',
        available_bytes: 8000000000,
      }),
    }),
  );
  await page.route('**/api/v1/sessions', async (r) => {
    if (r.request().method() !== 'POST') return r.continue();
    writes.push('sessions');
    const { streams } = r.request().postDataJSON();
    expect(streams).toHaveLength(1);
    expect(streams[0].settings.adapter).toBe('tinysa-serial');
    await r.fulfill({ json: envelope({ id: upperSession, streams }) });
  });
  await page.route('**/api/v1/monitoring-projects', async (r) => {
    if (r.request().method() !== 'POST') return r.continue();
    writes.push('monitoring-projects');
    created = true;
    await r.fulfill({ json: envelope(upper) });
  });
  await page.route('**/api/v1/monitoring-projects?*', (r) =>
    r.fulfill({ json: envelopePage(created ? [upper] : []) }),
  );
  await page.route('**/api/v1/monitoring-projects/' + upperProject + '/**', async (r) => {
    const path = new URL(r.request().url()).pathname.split(upperProject + '/')[1];
    if (r.request().method() === 'POST') {
      writes.push(path);
      if (path === 'preparation/start') collecting = true;
      if (path === 'scan-plan')
        expect(r.request().postDataJSON().bands[0]).toMatchObject({
          start_hz: 900000000,
          stop_hz: 901500000,
          step_hz: 10000,
        });
      return r.fulfill({
        json: envelope(
          path === 'preparation/start' ? { project: { ...upper, state: 'collecting' } } : upper,
        ),
      });
    }
    const data =
      path === 'preflight'
        ? {
            ready: false,
            preparation_ready: true,
            baseline_ready: false,
            checks: [
              {
                name: 'measurement_qualification',
                ok: false,
                detail: 'No eligible automatic-duty windows',
              },
            ],
            baseline_collection: { bands: [], ready: false },
          }
        : path === 'status'
          ? {
              project: { ...upper, state: collecting ? 'collecting' : 'draft' },
              active: false,
              workers: [],
              manual: [],
            }
          : path === 'search/status'
            ? { active: false, marks: [], series: [] }
            : path === 'storage'
              ? { ready: true }
              : { items: [], next_cursor: null };
    await r.fulfill({ json: envelope(data) });
  });
  await page.route('**/api/v1/sessions/' + upperSession + '/sweeps?*', (r) =>
    r.fulfill({ json: envelope([]) }),
  );
  await openDesk(page);
  await page.getByRole('button', { name: 'Prepare project' }).click();
  const wizard = page.getByRole('dialog', { name: 'Prepare a receive-only project' });
  await wizard.getByLabel('Project name').fill(upper.name);
  await wizard.getByLabel('Site or location').fill('Synthetic');
  await wizard.getByLabel('tinySA serial port').fill('SYNTHETIC-NO-OPEN');
  await wizard.getByRole('button', { name: 'Next', exact: true }).click();
  await wizard.getByLabel('Start (MHz)').fill('900');
  await wizard.getByLabel('Stop (MHz)').fill('901.5');
  await expect(wizard).toContainText('observation-only');
  await wizard.getByRole('button', { name: 'Next', exact: true }).click();
  await wizard.getByLabel('Archive on the recording node').selectOption('node-archive');
  await wizard.getByLabel('Antenna and receive path notes').fill('Synthetic fixture');
  await wizard.getByLabel('Receive path and antenna checked').check();
  await wizard.getByLabel('Location and ordinary RF environment checked').check();
  await wizard.getByRole('button', { name: 'Start receive collection' }).click();
  await expect.poll(() => collecting).toBe(true);
  await wizard.getByRole('button', { name: 'Close setup' }).click();
  await expect(page.getByRole('button', { name: 'Arm guard', exact: true })).toBeDisabled();
  expect(writes).toEqual([
    'sessions',
    'monitoring-projects',
    'scan-plan',
    'preparation',
    'preparation/start',
  ]);
});

test('scheduled stop failure keeps its reason and stop action available in both languages', async ({
  page,
}) => {
  const state = {
    ...project,
    state: 'collecting',
    duty: { enabled: true, status: 'stop_failed', last_error: 'Synthetic receiver could not stop' },
  };
  await page.route('**/api/v1/capabilities', async (r) => {
    const response = await r.fetch(),
      data = (await response.json()).data;
    Object.assign(data.features, { single_source_duty: true, monitoring_duty_schedule: true });
    await r.fulfill({ response, json: envelope(data) });
  });
  await page.addInitScript((id) => localStorage.setItem('ori-modern-project', id), pid);
  await page.route('**/api/v1/monitoring-projects?*', (r) =>
    r.fulfill({ json: envelope({ items: [state], next_cursor: null }) }),
  );
  await page.route('**/api/v1/monitoring-projects/' + pid + '/**', (r) => {
    const key = new URL(r.request().url()).pathname.split(pid + '/')[1];
    const data =
      key === 'status'
        ? { project: state, active: false, workers: [], manual: [] }
        : key === 'preflight'
          ? { ready: true, baseline_ready: true, checks: [], baseline_collection: { ready: true } }
          : key === 'duty'
            ? {
                ...state.duty,
                schedule: { mode: 'continuous' },
                resume_after_restart: false,
                deliveries: [],
              }
            : key === 'search/status'
              ? { active: false, series: [], marks: [] }
              : { items: [], next_cursor: null };
    return r.fulfill({ json: envelope(data) });
  });
  await openDesk(page);
  await expect(page.getByTestId('guard-state')).toHaveText('Collection stop unconfirmed');
  await expect(
    page.getByText('Synthetic receiver could not stop', { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop duty', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Duty setup' }).click();
  const wizard = page.getByRole('dialog', { name: 'Prepare a receive-only project' });
  await expect(wizard.getByRole('button', { name: 'Retry duty' })).toBeEnabled();
  await expect(wizard.getByRole('button', { name: 'Stop duty' })).toBeEnabled();
  await wizard.getByRole('button', { name: 'Close setup' }).click();
  await page.getByTestId('locale-select').selectOption('ru');
  await expect(page.getByTestId('guard-state')).toHaveText('Остановка записи не подтверждена');
});
