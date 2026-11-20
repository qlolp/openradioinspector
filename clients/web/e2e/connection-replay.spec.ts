import { expect, test } from '@playwright/test';
import { openDesk } from './helpers';

test('returning from connection settings replays missed sources from the applied cursor and keeps its bounded view', async ({
  page,
  request,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem('ori-modern-locale', 'en');
    const NativeWorker = window.Worker;
    window.Worker = class extends NativeWorker {
      constructor(...args: ConstructorParameters<typeof Worker>) {
        super(...args);
        this.addEventListener('message', ({ data }) => {
          if (data?.frame?.sweep?.id === 'checkpoint-before')
            document.documentElement.dataset.checkpointParsed = 'true';
        });
      }
    };
  });
  const base = 'http://127.0.0.1:18771/api/v1';
  const template = (await (await request.get(base + '/sessions')).json()).data[0];
  const created = await request.post(base + '/sessions', {
    data: {
      name: 'Owned connection checkpoint',
      streams: template.streams.map(({ id, session_id, state, ...stream }: any) => stream),
    },
  });
  expect(created.ok()).toBe(true);
  const session = (await created.json()).data;
  const projectResponse = await request.post(base + '/monitoring-projects', {
    data: {
      name: 'Owned connection checkpoint',
      site: 'Synthetic unarmed only',
      session_id: session.id,
    },
  });
  expect(projectResponse.ok()).toBe(true);
  const project = (await projectResponse.json()).data;
  const cursors: string[] = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/v1/events')
      cursors.push(new URL(request.url()).searchParams.get('after')!);
  });
  await openDesk(page);
  const select = page.getByTestId('project-select');
  await expect(select.locator('option', { hasText: 'Owned connection checkpoint' })).toHaveCount(1);
  const other = await select
    .locator('option')
    .evaluateAll(
      (options, target) =>
        options
          .map((option) => (option as HTMLOptionElement).value)
          .find((id) => id && id !== target),
      project.id,
    );
  expect(other).toBeTruthy();
  await select.selectOption(other!);
  const statusPath = '**/api/v1/monitoring-projects/' + project.id + '/status';
  let resumeStatus!: () => void;
  const statusHeld = new Promise<void>((resolve) => {
    resumeStatus = resolve;
  });
  await page.route(statusPath, async (route) => {
    await statusHeld;
    await route.continue().catch(() => {});
  });
  await select.selectOption(project.id);
  const put = async (id: string) => {
    const response = await request.post(base + '/sessions/' + session.id + '/sweeps', {
      data: {
        id,
        stream_id: session.streams[0].id,
        interval: {
          started_at: '2026-11-11T12:00:00.123456Z',
          ended_at: '2026-11-11T12:00:00.223456Z',
        },
        frequencies_hz: [100000000, 100000001],
        levels: [-80, -60],
        level_unit: 'dBm',
        settings: { synthetic: true },
      },
    });
    expect(response.ok()).toBe(true);
  };
  try {
    await put('checkpoint-before');
    // The SSE source reached the real parser while status and stream IDs are still unknown.
    await expect(page.locator('html')).toHaveAttribute('data-checkpoint-parsed', 'true');
  } finally {
    resumeStatus();
    await page.unroute(statusPath);
  }
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
    'data-sweep-id',
    'checkpoint-before',
  );
  await expect(page.locator('.project-meta')).toContainText(session.id);
  const applied = (await (await request.get(base + '/health')).json()).data.last_event_id;
  await page.getByRole('button', { name: 'Connection settings', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Return to workspace', exact: true }),
  ).toBeVisible();
  await put('checkpoint-missed-a');
  await put('checkpoint-missed-b');
  let sourceReads = 0;
  page.on('request', (request) => {
    if (/\/sessions\/[^/]+\/(sweeps|observations)\?/.test(request.url())) sourceReads++;
  });
  await page.getByRole('button', { name: 'Return to workspace', exact: true }).click();
  await expect(page.getByTestId('workspace-heading')).toBeVisible();
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
    'data-sweep-id',
    'checkpoint-missed-b',
  );
  await expect(page.locator('.waterfall')).toHaveAttribute(
    'aria-label',
    'Waterfall, 3 committed rows, newest at top',
  );
  expect(cursors.at(-1)).toBe(String(applied));
  expect(sourceReads).toBe(0);
  await expect(page.getByTestId('guard-state')).toHaveText('Draft');
});

test('hydrates a changed source scope before replay when its ordinary status refresh is delayed', async ({
  page,
  request,
}) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
  const base = 'http://127.0.0.1:18771/api/v1';
  const template = (await (await request.get(base + '/sessions')).json()).data[0];
  const create = async (name: string) => {
    const response = await request.post(base + '/sessions', {
      data: {
        name,
        streams: template.streams.map(({ id, session_id, state, ...stream }: any) => stream),
      },
    });
    expect(response.ok()).toBe(true);
    return (await response.json()).data;
  };
  const original = await create('Owned scope before checkpoint');
  const project = (
    await (
      await request.post(base + '/monitoring-projects', {
        data: {
          name: 'Owned scope transition',
          site: 'Synthetic unarmed only',
          session_id: original.id,
        },
      })
    ).json()
  ).data;
  const post = async (session: any, id: string) => {
    const response = await request.post(base + '/sessions/' + session.id + '/sweeps', {
      data: {
        id,
        stream_id: session.streams[0].id,
        interval: {
          started_at: '2026-11-11T12:00:00.123456Z',
          ended_at: '2026-11-11T12:00:00.223456Z',
        },
        frequencies_hz: [100000000, 100000001],
        levels: [-80, -60],
        level_unit: 'dBm',
        settings: { synthetic: true },
      },
    });
    expect(response.ok()).toBe(true);
  };
  // A remembered project ID is available before the first page of summaries.
  await page.addInitScript((id) => localStorage.setItem('ori-modern-project', id), project.id);
  let releaseList!: () => void;
  const listHeld = new Promise<void>((resolve) => {
    releaseList = resolve;
  });
  const listPath = '**/api/v1/monitoring-projects?*';
  await page.route(listPath, async (route) => {
    await listHeld;
    await route.continue().catch(() => {});
  });
  const listRequested = page.waitForRequest(listPath);
  try {
    await openDesk(page);
    await listRequested;
    await post(original, 'scope-before');
  } finally {
    releaseList();
    await page.unroute(listPath);
  }
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
    'data-sweep-id',
    'scope-before',
  );
  await page.getByRole('button', { name: 'Connection settings', exact: true }).click();
  const replacement = await create('Owned reported source search');
  await post(replacement, 'scope-missed-a');
  await post(replacement, 'scope-missed-b');
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  let reads = 0;
  // The transport fixture reports a source-search transition; all session/source
  // records and their committed events above are real temporary core data. No
  // worker or hardware is started for this ordering fault injection.
  await page.route(
    '**/api/v1/monitoring-projects/' + project.id + '/search/status',
    async (route) => {
      if (++reads === 1) await delayed;
      await route
        .fulfill({
          json: {
            contract: 'openradioinspector-platform',
            contract_version: 1,
            data: {
              active: true,
              session_id: replacement.id,
              stream_id: replacement.streams[0].id,
              coverage: 'narrow',
              series: [],
              marks: [],
            },
          },
        })
        .catch(() => {});
    },
  );
  try {
    await page.getByRole('button', { name: 'Return to workspace', exact: true }).click();
    await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
      'data-sweep-id',
      'scope-missed-b',
    );
    await expect(page.locator('.waterfall')).toHaveAttribute(
      'aria-label',
      'Waterfall, 2 committed rows, newest at top',
    );
    expect(reads).toBeGreaterThanOrEqual(2);
  } finally {
    release();
  }
});
