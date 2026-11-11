import { expect, test } from '@playwright/test';
import { openDesk } from './helpers';

test('replays more than 1000 durable sweeps through a slow worker after disconnect without reseeding or skipping', async ({
  page,
  request,
}) => {
  test.setTimeout(60000);
  await page.addInitScript(() => {
    localStorage.setItem('ori-modern-locale', 'en');
    const stats = { active: 0, maximum: 0, applied: [] as string[] };
    (window as any).__replay = stats;
    const OriginalWorker = Worker;
    (window as any).Worker = new Proxy(OriginalWorker, {
      construct(Target, args) {
        const worker = new Target(...(args as [string, WorkerOptions]));
        const setter = Object.getOwnPropertyDescriptor(OriginalWorker.prototype, 'onmessage')!.set!;
        Object.defineProperty(worker, 'onmessage', {
          set(handler) {
            setter.call(worker, (event: MessageEvent) =>
              setTimeout(() => {
                stats.active--;
                handler.call(worker, event);
                const id = event.data.frame?.sweep?.id;
                if (id?.startsWith('durable-replay-')) stats.applied.push(id);
              }, 2),
            );
          },
        });
        const post = worker.postMessage.bind(worker);
        worker.postMessage = ((...values: Parameters<Worker['postMessage']>) => {
          stats.active++;
          stats.maximum = Math.max(stats.maximum, stats.active);
          post(...values);
        }) as Worker['postMessage'];
        return worker;
      },
    });
  });
  const base = 'http://127.0.0.1:18771/api/v1';
  const template = (await (await request.get(base + '/sessions')).json()).data[0];
  const created = await request.post(base + '/sessions', {
    data: {
      name: 'Owned durable replay test',
      streams: template.streams.map(({ id, session_id, state, ...stream }: any) => stream),
    },
  });
  expect(created.ok()).toBe(true);
  const session = (await created.json()).data;
  const projectResponse = await request.post(base + '/monitoring-projects', {
    data: {
      name: 'Owned durable replay test',
      site: 'Synthetic unarmed only',
      session_id: session.id,
    },
  });
  expect(projectResponse.ok()).toBe(true);
  const project = (await projectResponse.json()).data;
  const cursors: string[] = [];
  let release!: () => void, held!: () => void;
  const blocked = new Promise<void>((resolve) => {
    release = resolve;
  });
  const waiting = new Promise<void>((resolve) => {
    held = resolve;
  });
  await page.route('**/api/v1/events?after=*', async (route) => {
    cursors.push(new URL(route.request().url()).searchParams.get('after')!);
    if (cursors.length === 1)
      await route.fulfill({
        contentType: 'text/event-stream',
        body: ': forced owned disconnect\n\n',
      });
    else {
      held();
      await blocked;
      await route.continue();
    }
  });
  await openDesk(page);
  const seeded = page.waitForResponse((response) =>
    response.url().includes('/sessions/' + session.id + '/sweeps?'),
  );
  await page.getByTestId('project-select').selectOption(project.id);
  await seeded;
  await waiting;
  await expect(page.getByTestId('guard-state')).toHaveText('Draft');
  // Wait for the initial empty seed before producing any source. Recovery must
  // consume the committed archive cursor, never another latest-one snapshot.
  await expect(page.getByTestId('source-id')).toHaveText('Waiting for a committed sweep');
  const sourceReads: string[] = [];
  page.on('request', (r) => {
    if (/\/sessions\/[^/]+\/(sweeps|observations)\?/.test(r.url())) sourceReads.push(r.url());
  });
  const ids = Array.from(
    { length: 1205 },
    (_, i) => 'durable-replay-' + String(i).padStart(4, '0'),
  );
  for (let offset = 0; offset < ids.length; offset += 50) {
    const response = await request.post(base + '/sessions/' + session.id + '/sweeps/batch', {
      data: {
        items: ids.slice(offset, offset + 50).map((id) => ({
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
        })),
      },
    });
    expect(response.ok(), await response.text()).toBe(true);
  }
  release();
  await expect
    .poll(() => page.evaluate(() => (window as any).__replay.applied.length), { timeout: 30000 })
    .toBe(1205);
  const stats = await page.evaluate(() => (window as any).__replay);
  expect(stats.applied).toEqual(ids);
  expect(stats.maximum).toBe(1);
  expect(cursors[1]).toBe(cursors[0]);
  expect(sourceReads).toEqual([]);
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
    'data-sweep-id',
    ids.at(-1)!,
  );
  await expect(page.locator('.waterfall')).toHaveAttribute(
    'aria-label',
    'Waterfall, 256 committed rows, newest at top',
  );
  await expect(page.getByText(/client sequence\/backlog gap/)).toHaveCount(0);
});
