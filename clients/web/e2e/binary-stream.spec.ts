import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';
import { originalJSON, parseWire, sweepValue } from '../src/wire';
import type { JSONObject } from '../../../contracts/generated/typescript/index.ts';
import { openDesk } from './helpers';
import { advertiseBinary, CORE, ownProject, observeBinary } from './binary-fixture';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
});
const interval = {
  started_at: '2026-11-11T12:00:00.123456Z',
  ended_at: '2026-11-11T12:00:00.223456Z',
};

test('negotiated binary replay transfers 1205 committed sources through one slow worker without a seed refresh', async ({
  page,
  request,
}) => {
  test.setTimeout(60000);
  await page.addInitScript(() => {
    const stats = {
      active: 0,
      maximum: 0,
      detached: 0,
      applied: [] as string[],
      typed: 0,
      pendingAt: 0,
      timing: [] as { worker: number; delayedApplication: number }[],
    };
    (window as any).__binary = stats;
    const Original = Worker,
      setter = Object.getOwnPropertyDescriptor(Worker.prototype, 'onmessage')!.set!;
    (window as any).Worker = new Proxy(Original, {
      construct(Target, args) {
        const worker = new Target(...(args as [string, WorkerOptions]));
        Object.defineProperty(worker, 'onmessage', {
          set(handler) {
            setter.call(worker, (event: MessageEvent) => {
              const receivedAt = performance.now();
              setTimeout(() => {
                if (stats.timing.length < 2048)
                  stats.timing.push({
                    worker: receivedAt - stats.pendingAt,
                    delayedApplication: performance.now() - receivedAt,
                  });
                stats.active--;
                const sweep = event.data.frame?.sweep;
                if (sweep?.id.startsWith('binary-replay-')) {
                  stats.applied.push(sweep.id);
                  if (
                    sweep.levels instanceof Float64Array &&
                    sweep.frequencies_hz instanceof Float64Array &&
                    sweep.source_indices instanceof Uint32Array
                  )
                    stats.typed++;
                }
                handler.call(worker, event);
              }, 2);
            });
          },
        });
        const post = worker.postMessage.bind(worker);
        worker.postMessage = ((...values: Parameters<Worker['postMessage']>) => {
          stats.active++;
          stats.pendingAt = performance.now();
          stats.maximum = Math.max(stats.maximum, stats.active);
          const raw = values[0].raw;
          if (!(raw instanceof ArrayBuffer))
            throw Error('Binary transport converted frame into text');
          post(...values);
          if (!raw.byteLength) stats.detached++;
        }) as Worker['postMessage'];
        return worker;
      },
    });
  });
  const originalProjects = await request.get(CORE + '/monitoring-projects?limit=100');
  expect(originalProjects.ok()).toBe(true);
  const initialProject = (await originalProjects.json()).data.items[0];
  expect(initialProject?.active_session_id).toBeTruthy();
  const { session, project } = await ownProject(request, 'Owned binary replay');
  expect(initialProject.id).not.toBe(project.id);
  await page.addInitScript(
    (id: string) => localStorage.setItem('ori-modern-project', id),
    initialProject.id,
  );
  await advertiseBinary(page);
  await observeBinary(page, true);
  let sseRequests = 0;
  page.on('request', (r) => {
    if (new URL(r.url()).pathname === '/api/v1/events') sseRequests++;
  });
  const initialSeed = page.waitForResponse((r) =>
    r.url().includes('/sessions/' + initialProject.active_session_id + '/sweeps?'),
  );
  await openDesk(page);
  await initialSeed;
  await expect(page.getByTestId('project-select')).toHaveValue(initialProject.id);
  // The workspace heading precedes asynchronous project/subscription setup.
  // Observe the intentionally disconnected first socket before changing scope.
  await expect.poll(() => page.evaluate(() => (window as any).__socket.opened)).toBe(1);
  const seed = page.waitForResponse((r) =>
    r.url().includes('/sessions/' + session.id + '/sweeps?'),
  );
  await page.getByTestId('project-select').selectOption(project.id);
  await seed;
  await expect.poll(() => page.evaluate(() => (window as any).__socket.opened)).toBe(2);
  await expect(page.getByTestId('source-id')).toHaveText('Waiting for a committed sweep');
  await expect(page.getByTestId('guard-state')).toHaveText('Draft');
  const sourceReads: string[] = [];
  page.on('request', (r) => {
    if (/\/sessions\/[^/]+\/(sweeps|observations)\?/.test(r.url())) sourceReads.push(r.url());
  });
  const ids = Array.from({ length: 1205 }, (_, i) => 'binary-replay-' + String(i).padStart(4, '0'));
  const initial = await page.evaluate(() => (window as any).__socket.cursors[1]);
  let sequence = BigInt(initial);
  for (let offset = 0; offset < ids.length; offset += 50) {
    const response = await request.post(CORE + '/sessions/' + session.id + '/sweeps/batch', {
      data: {
        items: ids.slice(offset, offset + 50).map((id) => ({
          id,
          stream_id: session.streams[0].id,
          interval,
          frequencies_hz: [100000000, 100000001],
          levels: [-80, -60],
          level_unit: 'dBm',
          settings: { synthetic: true },
        })),
      },
    });
    expect(response.ok(), await response.text()).toBe(true);
    sequence += BigInt((await response.json()).data.count);
  }
  expect(BigInt((await (await request.get(CORE + '/health')).json()).data.last_event_id)).toBe(
    sequence,
  );
  try {
    await expect
      .poll(() => page.evaluate(() => (window as any).__socket.acknowledged.length), {
        timeout: 30000,
      })
      .toBe(ids.length);
  } finally {
    await test.info().attach('bounded-functional-replay-diagnostics', {
      contentType: 'application/json',
      body: JSON.stringify(
        await page.evaluate(() => ({
          socket: (window as any).__socket,
          worker: (window as any).__binary,
        })),
      ),
    });
  }
  const stats = await page.evaluate(() => (window as any).__binary);
  const socket = await page.evaluate(() => (window as any).__socket);
  expect(stats.applied).toEqual(ids);
  expect(stats.maximum).toBe(1);
  expect(stats.detached).toBe(1205);
  expect(stats.typed).toBe(1205);
  expect(socket.cursors[1]).toBe(socket.cursors[0]);
  expect(socket.protocols).toEqual([
    'ori.measurements.v1.protobuf',
    'ori.measurements.v1.protobuf',
  ]);
  expect(socket.acknowledged).toEqual(
    Array.from({ length: 1205 }, (_, index) => (BigInt(initial) + BigInt(index + 1)).toString()),
  );
  expect(sourceReads).toEqual([]);
  expect(sseRequests).toBe(0);
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

test('valid future-range sources are acknowledged with exact original evidence and do not stall later binary sweeps', async ({
  page,
  request,
}) => {
  const { session, project } = await ownProject(request, 'Owned future-range source');
  await advertiseBinary(page);
  await observeBinary(page);
  await openDesk(page);
  const seed = page.waitForResponse((r) =>
    r.url().includes('/sessions/' + session.id + '/sweeps?'),
  );
  await page.getByTestId('project-select').selectOption(project.id);
  await seed;
  await expect.poll(() => page.evaluate(() => (window as any).__socket.opened)).toBe(1);
  const initial = BigInt(await page.evaluate(() => (window as any).__socket.cursors[0]));
  const unsafe =
    '{"id":"future-range-evidence","stream_id":' +
    JSON.stringify(session.streams[0].id) +
    ',"interval":' +
    JSON.stringify(interval) +
    ',"frequencies_hz":[9007199254740993,9007199254740994],"levels":[-0.0,-60.0],"level_unit":"dBm","settings":{"synthetic":true,"$serde_json::private::Number":{"$serde_json::private::RawValue":"literal"},"huge":18446744073709551616000}}';
  const response = await request.post(CORE + '/sessions/' + session.id + '/sweeps', {
    data: unsafe,
    headers: { 'Content-Type': 'application/json' },
  });
  expect(response.ok(), await response.text()).toBe(true);
  // Current legacy history filtering is explicitly capped at20GHz. Exercise a
  // future precise history response with this real archived original, without
  // claiming wider server filtering support or altering that policy.
  const historySource = originalJSON(
    sweepValue((parseWire(await response.text()) as JSONObject).data),
  );
  await page.route('**/api/v1/monitoring-projects/' + project.id + '/history?*', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body:
        '{"contract":"openradioinspector-platform","contract_version":1,"data":{"items":[' +
        historySource +
        '],"next_cursor":null}}',
    }),
  );
  const sequence = initial + 1n;
  await expect
    .poll(() => page.evaluate(() => (window as any).__socket.acknowledged))
    .toEqual([sequence.toString()]);
  const unsupported = page.getByTestId('unsupported-source');
  await expect(unsupported).toContainText('Source frequency outside supported display range');
  await page.screenshot({
    path: test.info().outputPath('binary-source-only-display.png'),
    fullPage: true,
  });
  await unsupported.getByRole('button', { name: 'Original sweep', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('9007199254740993');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download original JSON' }).click();
  const file = await download,
    stream = await file.createReadStream(),
    chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(chunk as Buffer);
  const exported = Buffer.concat(chunks).toString('utf8');
  expect(exported).toContain('9007199254740994');
  expect(exported).toContain('18446744073709551616000');
  expect(exported).toContain('"levels":[-0.0,-60.0]');
  expect(exported).toContain('"$serde_json::private::RawValue":"literal"');
  await page.getByRole('button', { name: 'Close original', exact: true }).click();
  await page.getByRole('button', { name: 'Research', exact: true }).click();
  await page.getByRole('button', { name: 'Load history', exact: true }).click();
  const unsupportedHistory = page.getByTestId('research-unsupported-source');
  await expect(unsupportedHistory).toContainText(
    'Source frequency outside supported display range',
  );
  await unsupportedHistory.getByRole('button', { name: 'Original sweep', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('9007199254740993');
  await page.getByRole('button', { name: 'Close original', exact: true }).click();
  await page.getByRole('button', { name: 'Monitoring', exact: true }).click();
  const next = await request.post(CORE + '/sessions/' + session.id + '/sweeps', {
    data: {
      id: 'supported-after-future-range',
      stream_id: session.streams[0].id,
      interval,
      frequencies_hz: [100000000, 100000001],
      levels: [-80, -60],
      level_unit: 'dBm',
      settings: { synthetic: true },
    },
  });
  expect(next.ok(), await next.text()).toBe(true);
  await expect
    .poll(() => page.evaluate(() => (window as any).__socket.acknowledged.length))
    .toBe(2);
  await expect(page.getByTestId('unsupported-source')).toHaveCount(0);
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
    'data-sweep-id',
    'supported-after-future-range',
  );
  await page.screenshot({
    path: test.info().outputPath('binary-supported-after-future-range.png'),
    fullPage: true,
  });
});

test('binary packed sweeps and a committed generic WAV event preserve source order, peak ties and both waterfall rows', async ({
  page,
  request,
}) => {
  await page.addInitScript(() => {
    const Original = Worker,
      setter = Object.getOwnPropertyDescriptor(Worker.prototype, 'onmessage')!.set!;
    const frames: { type: string; id?: string; firstPeak?: number; typed?: boolean }[] = [];
    (window as any).__audioFrames = frames;
    (window as any).Worker = new Proxy(Original, {
      construct(Target, args) {
        const worker = new Target(...(args as [string, WorkerOptions]));
        Object.defineProperty(worker, 'onmessage', {
          set(handler) {
            setter.call(worker, (message: MessageEvent) => {
              const frame = message.data.frame;
              if (frame)
                frames.push({
                  type: frame.event_type,
                  id: frame.sweep?.id,
                  firstPeak: frame.sweep?.peak.index,
                  typed: frame.sweep?.levels instanceof Float64Array,
                });
              handler.call(worker, message);
            });
          },
        });
        return worker;
      },
    });
  });
  const { session, project } = await ownProject(request, 'Owned packed/generic stream');
  await advertiseBinary(page);
  await observeBinary(page);
  await openDesk(page);
  const seed = page.waitForResponse((response) =>
    response.url().includes('/sessions/' + session.id + '/sweeps?'),
  );
  await page.getByTestId('project-select').selectOption(project.id);
  await seed;
  await expect.poll(() => page.evaluate(() => (window as any).__socket.opened)).toBe(1);
  let sourceReads = 0;
  page.on('request', (r) => {
    if (/\/sessions\/[^/]+\/(sweeps|observations)\?/.test(r.url())) sourceReads++;
  });
  const source = (id: string, level: number) => ({
    id,
    stream_id: session.streams[0].id,
    interval,
    frequencies_hz: Array.from({ length: 4096 }, (_, index) => 100000000 + index),
    levels: Array.from({ length: 4096 }, (_, index) =>
      index === 5 || index === 4000 ? level : -100,
    ),
    level_unit: 'dBm',
    settings: { synthetic: true },
  });
  const first = await request.post(CORE + '/sessions/' + session.id + '/sweeps', {
    data: source('binary-before-audio', -50),
  });
  expect(first.ok(), await first.text()).toBe(true);
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
    'data-sweep-id',
    'binary-before-audio',
  );
  const wav = Buffer.alloc(44 + 1600);
  wav.write('RIFF');
  wav.writeUInt32LE(wav.length - 8, 4);
  wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(8000, 24);
  wav.writeUInt32LE(16000, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write('data', 36);
  wav.writeUInt32LE(1600, 40);
  const audio = await request.post(CORE + '/sessions/' + session.id + '/audio-segments', {
    data: {
      id: 'binary-generic-audio',
      stream_id: session.streams[1].id,
      interval,
      frequency_hz: 100000000,
      mode: 'FM',
      data_base64: wav.toString('base64'),
      sha256: createHash('sha256').update(wav).digest('hex'),
    },
  });
  expect(audio.ok(), await audio.text()).toBe(true);
  const second = await request.post(CORE + '/sessions/' + session.id + '/sweeps', {
    data: source('binary-after-audio', -60),
  });
  expect(second.ok(), await second.text()).toBe(true);
  await expect
    .poll(() => page.evaluate(() => (window as any).__socket.acknowledged.length))
    .toBe(3);
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
    'data-sweep-id',
    'binary-after-audio',
  );
  await expect(page.locator('.waterfall')).toHaveAttribute(
    'aria-label',
    'Waterfall, 2 committed rows, newest at top',
  );
  expect(await page.evaluate(() => (window as any).__audioFrames)).toEqual([
    { type: 'sweep.received', id: 'binary-before-audio', firstPeak: 5, typed: true },
    { type: 'audio_segment.received', id: undefined, firstPeak: undefined, typed: false },
    { type: 'sweep.received', id: 'binary-after-audio', firstPeak: 5, typed: true },
  ]);
  expect(sourceReads).toBe(0);
  await expect(page.getByText(/client sequence\/backlog gap/)).toHaveCount(0);
});
