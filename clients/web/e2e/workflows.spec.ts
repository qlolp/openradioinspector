import { expect, test } from '@playwright/test';

test('monitoring opens committed spectrum, arms safely, and preserves independent manual ownership', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('./');
  await expect(page.getByRole('heading', { name: 'Spectrum desk' })).toBeVisible();
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  await expect(page.getByText('Quiet reference ready')).toBeVisible();
  await page.getByRole('button', { name: 'Arm guard', exact: true }).click();
  await expect(page.getByTestId('guard-state')).toHaveText('Armed');
  await expect(page.getByTestId('live-state')).toContainText('Live');
  await expect(page.getByTestId('source-id')).not.toHaveText('Waiting for a committed sweep');
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute('data-sweep-id', /.+/);
  await page.getByRole('button', { name: 'Research', exact: true }).click();
  await page.getByRole('button', { name: 'Take receiver control' }).click();
  await expect(page.getByText('Receiver lease · active')).toBeVisible();
  await page.getByLabel('Tune frequency (Hz)').fill('101500000');
  await page.getByRole('button', { name: 'Tune receiver', exact: true }).click();
  await expect(page.getByTestId('action-state')).toContainText('Confirmed');
  await page.getByRole('button', { name: 'Return receiver' }).click();
  await page.getByRole('button', { name: 'Disarm guard', exact: true }).click();
  await expect(page.getByTestId('guard-state')).toHaveText('Ready');
  expect(errors).toEqual([]);
});

test('event original/audio are at most two actions away; history filters and comparison use core data', async ({
  page,
}) => {
  await page.goto('./');
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  const eventRow = page.getByTestId('event-row').first();
  await eventRow.getByRole('button', { name: 'Details' }).click();
  const dialog = page.getByRole('dialog', { name: 'Event details' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Play audio' }).click();
  await expect(dialog.locator('audio')).toHaveAttribute('src', /^blob:/);
  await dialog.getByRole('button', { name: 'Original sweep' }).click();
  await expect(page.getByRole('dialog', { name: 'Original sweep' })).toBeVisible();
  await expect(
    page
      .getByRole('dialog', { name: 'Original sweep' })
      .getByText('201 original points', { exact: true }),
  ).toBeVisible();
  await page.getByRole('button', { name: 'Close original' }).click();
  await page.getByRole('button', { name: 'Close event' }).click();
  await page.getByRole('button', { name: 'Research', exact: true }).click();
  await page.getByRole('button', { name: 'Load history', exact: true }).click();
  await expect(page.getByTestId('history-row')).toHaveCount(32);
  await page.getByRole('button', { name: 'Next history page' }).click();
  await expect(page.getByTestId('history-row').first()).toBeVisible();
  await page
    .getByTestId('history-row')
    .first()
    .getByRole('button', { name: 'Compare baseline' })
    .click();
  await expect(page.getByText('Baseline comparison · compatible')).toBeVisible();
  await page.getByLabel('Minimum frequency (Hz)').fill('200000000');
  await page.getByRole('button', { name: 'Load history', exact: true }).click();
  await expect(page.getByText('No sweeps match these filters')).toBeVisible();
});

test('receive-only preparation wizard creates a project without starting workers implicitly', async ({
  page,
}) => {
  await page.goto('./');
  await page.getByRole('button', { name: 'Prepare project' }).click();
  const wizard = page.getByRole('dialog', { name: 'Prepare a receive-only project' });
  await wizard.getByLabel('Project name').fill('Browser prepared project');
  await wizard.getByLabel('Site or location').fill('Synthetic test bench');
  await wizard.getByLabel('Use explicit simulators').check();
  await wizard.getByRole('button', { name: 'Next: receive path' }).click();
  await wizard.getByLabel('Antenna and receive path notes').fill('Synthetic antenna');
  await wizard.getByLabel('Receive path and antenna checked').check();
  await wizard.getByLabel('Location and ordinary RF environment checked').check();
  await wizard.getByRole('button', { name: 'Create prepared project' }).click();
  await expect(page.getByLabel('Project', { exact: true })).toHaveValue(/.+/);
  await expect(page.getByTestId('guard-state')).toHaveText('Draft');
  await expect(page.getByRole('button', { name: 'Start receive collection' })).toBeVisible();
});

test('allowed ranges and narrow source search retain explicit analyzer ownership and restore broad coverage', async ({
  page,
}) => {
  await page.goto('./');
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  await page.getByLabel('Signal name').fill('Authorized laboratory beacon');
  await page.getByLabel('Allowed start (Hz)').fill('98500000');
  await page.getByLabel('Allowed end (Hz)').fill('98600000');
  await page.getByRole('button', { name: 'Add allowed signal', exact: true }).click();
  await expect(page.getByText('Authorized laboratory beacon', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Arm guard', exact: true }).click();
  await page.getByRole('button', { name: 'Research', exact: true }).click();
  await page.getByLabel('Instrument role').selectOption('analyzer');
  await page.getByRole('button', { name: 'Take analyzer control' }).click();
  await expect(page.getByText('Analyzer lease · active')).toBeVisible();
  await page.getByLabel('Source event', { exact: false }).selectOption({ index: 1 });
  await page.getByRole('button', { name: 'Start narrow search', exact: true }).click();
  await expect(page.getByText('Narrow search · broad coverage paused')).toBeVisible();
  await expect(page.locator('.receiver-frequency')).toContainText('Hz');
  await expect
    .poll(async () => page.locator('.source-footer').textContent())
    .not.toContain('Waiting for a committed sweep');
  await page.getByLabel('Position label').fill('Laboratory doorway');
  await page.getByRole('button', { name: 'Mark position', exact: true }).click();
  await expect(page.getByText('Laboratory doorway', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Return analyzer' }).click();
  await expect(page.getByText('Broad coverage active')).toBeVisible();
  await page.getByRole('button', { name: 'Disarm guard', exact: true }).click();
});

test('offline assets and local workspace survive reload; screenshots include light, dark and narrow layouts', async ({
  page,
}) => {
  const external: string[] = [];
  page.on('request', (r) => {
    if (!new URL(r.url()).hostname.match(/^(127\.0\.0\.1|localhost)$/)) external.push(r.url());
  });
  await page.goto('./');
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  await page.getByRole('button', { name: 'Arm guard', exact: true }).click();
  await expect(page.getByTestId('guard-state')).toHaveText('Armed');
  await expect
    .poll(async () =>
      Number(
        (await page.locator('.waterfall').getAttribute('aria-label'))?.match(
          /Waterfall, (\d+)/,
        )?.[1] || 0,
      ),
    )
    .toBeGreaterThan(8);
  await page.getByLabel('Theme').selectOption('dark');
  await page.screenshot({ path: 'test-results/monitoring-dark.png', fullPage: true });
  await page.getByRole('button', { name: 'Research', exact: true }).click();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Research', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await page.getByLabel('Theme').selectOption('light');
  await page.screenshot({ path: 'test-results/research-light.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('heading', { name: 'Spectrum desk' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/research-mobile.png', fullPage: true });
  expect(external).toEqual([]);
  await page.getByRole('button', { name: 'Disarm guard', exact: true }).click();
});

test('sequence gaps resynchronize explicitly and WebGL context loss uses the Canvas recovery path', async ({
  page,
}) => {
  let injected = false;
  await page.route('**/api/v1/events?after=*', async (route) => {
    if (injected) {
      await route.continue();
      return;
    }
    injected = true;
    const after = Number(new URL(route.request().url()).searchParams.get('after'));
    const frame = {
      contract: 'openradioinspector-platform',
      contract_version: 1,
      data: { sequence: after + 2, event_type: 'device.state', payload: {} },
    };
    await route.fulfill({
      contentType: 'text/event-stream',
      body: `id: ${after + 2}\nevent: device.state\ndata: ${JSON.stringify(frame)}\n\n`,
    });
  });
  await page.goto('./');
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  await expect(page.getByText(/1 client sequence\/backlog gap/)).toBeVisible();
  await page.getByRole('button', { name: 'Arm guard', exact: true }).click();
  await expect(page.getByTestId('live-state')).toContainText('Live');
  await expect(page.getByTestId('source-id')).not.toHaveText('Waiting for a committed sweep');
  const glAvailable = await page
    .locator('.waterfall canvas')
    .first()
    .evaluate((canvas) => {
      const gl = (canvas as HTMLCanvasElement).getContext('webgl2');
      const extension = gl?.getExtension('WEBGL_lose_context');
      (window as any).__restoreTestContext = () => extension?.restoreContext();
      extension?.loseContext();
      return !!extension;
    });
  if (glAvailable) {
    await expect(page.getByTestId('renderer-state')).toContainText('Canvas fallback');
    await page.evaluate(() => (window as any).__restoreTestContext());
    await expect(page.getByTestId('renderer-state')).toContainText('WebGL2');
  } else await expect(page.getByTestId('renderer-state')).toContainText('Canvas fallback');
  await page.getByRole('button', { name: 'Disarm guard', exact: true }).click();
});

test('256-row render buffers stay bounded without aggregate sweep refetches during committed ingest', async ({
  page,
  request,
}) => {
  await page.goto('./');
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  await expect(page.getByTestId('source-id')).not.toHaveText('Waiting for a committed sweep');
  const projects = await (
    await request.get('http://127.0.0.1:18772/api/v1/monitoring-projects')
  ).json();
  const project = projects.data.items.find((p: any) => p.name === 'North laboratory');
  const status = await (
    await request.get(`http://127.0.0.1:18772/api/v1/monitoring-projects/${project.id}/status`)
  ).json();
  const sweeps = await (
    await request.get(
      `http://127.0.0.1:18772/api/v1/sessions/${status.data.project.active_session_id}/sweeps?limit=1`,
    )
  ).json();
  const source = sweeps.data[0];
  const measurementFetches: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'GET' && /\/(sweeps|observations)\?/.test(r.url()))
      measurementFetches.push(r.url());
  });
  for (let batch = 0; batch < 64; batch++) {
    const data = Array.from({ length: 4 }, (_, i) => ({
      ...source,
      id: `bounded-${batch}-${i}`,
      interval: {
        started_at: new Date(Date.now() + batch * 10 + i).toISOString(),
        ended_at: new Date(Date.now() + batch * 10 + i + 1).toISOString(),
      },
    }));
    const response = await request.post(
      `http://127.0.0.1:18772/api/v1/sessions/${source.session_id}/sweeps/batch`,
      { data: { items: data } },
    );
    expect(response.ok()).toBe(true);
    const expected = Math.min(256, (batch + 1) * 4);
    await expect
      .poll(
        async () =>
          Number(
            (await page.locator('.waterfall').getAttribute('aria-label'))?.match(
              /Waterfall, (\d+)/,
            )?.[1] || 0,
          ),
        { message: `batch ${batch}: expected >=${expected} render rows` },
      )
      .toBeGreaterThanOrEqual(expected);
  }
  await expect(page.locator('.waterfall')).toHaveAttribute(
    'aria-label',
    'Waterfall, 256 committed rows, newest at top',
  );
  expect(measurementFetches).toEqual([]);
});

test('native bridge lifecycle connects once and keeps the parser alive across connection and node reconnect', async ({
  page,
}) => {
  await page.addInitScript(() => {
    let source: EventSource | undefined;
    let id = 0,
      node = '';
    (window as any).__TAURI_INTERNALS__ = {
      transformCallback: () => ++id,
      unregisterCallback: () => {},
      invoke: async (command: string, args: any) => {
        if (command === 'connect_node') {
          node = args.node;
          return null;
        }
        if (command === 'cancel_request' || command === 'open_legacy') return null;
        if (command === 'stop_events') {
          source?.close();
          return null;
        }
        if (command === 'api_request') {
          if (node.includes('empty-core.example')) {
            if (args.path.includes('/monitoring-projects/') || args.path.includes('/sessions/'))
              return JSON.stringify({
                contract: 'openradioinspector-platform',
                contract_version: 1,
                error: { code: 'not_found', message: 'This core has no projects' },
              });
            return JSON.stringify({
              contract: 'openradioinspector-platform',
              contract_version: 1,
              data:
                args.path === '/api/v1/health'
                  ? { status: 'ok', last_event_id: 0, sessions: 0 }
                  : args.path === '/api/v1/capabilities'
                    ? { features: {} }
                    : args.path === '/api/v1/sessions'
                      ? []
                      : { items: [], next_cursor: null },
            });
          }
          const response = await fetch(
            args.path,
            args.body == null
              ? {}
              : {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify(args.body),
                },
          );
          return response.text();
        }
        if (command === 'start_events') {
          if (node.includes('empty-core.example')) {
            source?.close();
            args.channel.onmessage({ kind: 'open' });
            return null;
          }
          source?.close();
          source = new EventSource('/api/v1/events?after=' + args.after);
          source.onopen = () => args.channel.onmessage({ kind: 'open' });
          source.onerror = () => args.channel.onmessage({ kind: 'reconnecting' });
          for (const type of [
            'session.created',
            'session.started',
            'session.stopped',
            'device.state',
            'device.tuned',
            'device.error',
            'sweep.received',
            'observation.received',
          ])
            source.addEventListener(type, (event) =>
              args.channel.onmessage({ kind: 'event', data: (event as MessageEvent).data }),
            );
          return null;
        }
        throw Error('Unexpected native command: ' + command);
      },
    };
  });
  await page.goto('./');
  await page.getByRole('button', { name: 'Connect node', exact: true }).click();
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  await page.getByRole('button', { name: 'Arm guard', exact: true }).click();
  await expect(page.getByTestId('guard-state')).toHaveText('Armed');
  await expect(page.getByTestId('source-id')).not.toHaveText('Waiting for a committed sweep');
  const first = await page.getByTestId('source-id').textContent();
  await expect
    .poll(async () => {
      const current = await page.getByTestId('source-id').textContent();
      return current !== first && current !== 'Waiting for a committed sweep';
    })
    .toBe(true);
  const next = await page.getByTestId('source-id').textContent();
  await expect
    .poll(async () => {
      const current = await page.getByTestId('source-id').textContent();
      return current !== next && current !== 'Waiting for a committed sweep';
    })
    .toBe(true);
  await page.getByRole('button', { name: 'Connect node', exact: true }).click();
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  await expect(page.getByTestId('guard-state')).toHaveText('Armed');
  const second = await page.getByTestId('source-id').textContent();
  await expect
    .poll(async () => {
      const current = await page.getByTestId('source-id').textContent();
      return current !== second && current !== 'Waiting for a committed sweep';
    })
    .toBe(true);
  await page.getByRole('button', { name: 'Disarm guard', exact: true }).click();
  await page.getByLabel('Node URL').fill('http://empty-core.example:8770');
  await page.getByRole('button', { name: 'Connect node', exact: true }).click();
  await expect(page.getByLabel('Project', { exact: true }).locator('option')).toHaveCount(1);
  await expect(page.getByTestId('guard-state')).toHaveText('Unavailable');
});

test('changing project cancels an old status read and never restores its previous guard state', async ({
  page,
  request,
}) => {
  await page.goto('./');
  const projects = (
    await (await request.get('http://127.0.0.1:18772/api/v1/monitoring-projects')).json()
  ).data.items;
  const north = projects.find((p: any) => p.name === 'North laboratory');
  const northStatus = await (
    await request.get(`http://127.0.0.1:18772/api/v1/monitoring-projects/${north.id}/status`)
  ).json();
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'Unprepared project' });
  await expect(page.getByTestId('guard-state')).toHaveText('Draft');
  let release: () => void = () => {};
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  let seen: () => void = () => {};
  const pending = new Promise<void>((resolve) => {
    seen = resolve;
  });
  await page.route(`**/monitoring-projects/${north.id}/status`, async (route) => {
    seen();
    await delayed;
    await route.fulfill({ json: northStatus }).catch(() => {});
  });
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  await pending;
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'Unprepared project' });
  release();
  await expect(page.getByTestId('guard-state')).toHaveText('Draft');
  await expect(page.getByText('Second synthetic scope', { exact: true })).toBeVisible();
});

test('changing project during gap snapshot recovery keeps the global subscription alive', async ({
  page,
  request,
}) => {
  const projectPage = (
    await (await request.get('http://127.0.0.1:18772/api/v1/monitoring-projects')).json()
  ).data.items;
  const north = projectPage.find((project: any) => project.name === 'North laboratory');
  await page.addInitScript((id) => {
    localStorage.setItem('ori-modern-project', id);
  }, north.id);
  let injected = false,
    reads = 0,
    release: () => void = () => {},
    seen: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const waiting = new Promise<void>((resolve) => {
    seen = resolve;
  });
  await page.route('**/api/v1/events?after=*', async (route) => {
    if (injected) {
      await route.continue();
      return;
    }
    injected = true;
    const after = Number(new URL(route.request().url()).searchParams.get('after'));
    await route.fulfill({
      contentType: 'text/event-stream',
      body: `event: device.state\ndata: ${JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data: { sequence: after + 2, event_type: 'device.state', payload: {} } })}\n\n`,
    });
  });
  await page.route('**/api/v1/sessions/*/sweeps?*', async (route) => {
    if (++reads === 2) {
      seen();
      await held;
    }
    await route.continue().catch(() => {});
  });
  await page.goto('./');
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'North laboratory' });
  await expect(page.getByText(/1 client sequence\/backlog gap/)).toBeVisible();
  await waiting;
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'Unprepared project' });
  release();
  await expect(page.getByTestId('guard-state')).toHaveText('Draft');
  const sessions = (await (await request.get('http://127.0.0.1:18772/api/v1/sessions')).json())
    .data;
  const streams = sessions[0].streams.map(({ id, session_id, state, ...stream }: any) => stream);
  await request.post('http://127.0.0.1:18772/api/v1/sessions', {
    data: { name: 'Recovery subscription probe', streams },
  });
  await expect(page.getByTestId('live-state')).toContainText('Live');
});
