import { openDesk } from './helpers';
import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
});

test('a committed audio event between two sweeps preserves both waterfall rows without a gap or resync', async ({
  page,
  request,
}) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type: string, ...args: unknown[]) {
      return type === 'webgl2' ? null : original.call(this, type, ...(args as []));
    } as typeof original;
  });
  const base = 'http://127.0.0.1:18771/api/v1';
  const sessionResponse = await request.post(base + '/sessions', {
    data: {
      name: 'Audio SSE regression',
      streams: [
        {
          device: 'tinySA Ultra',
          role: 'spectrum_analyzer',
          level_unit: 'dBm',
          port: 'sim://sse-analyzer',
          settings: {
            adapter: 'tinysa-sim',
            start_hz: 100000000,
            stop_hz: 100000001,
            points: 2,
            rbw_hz: 1000,
          },
        },
        {
          device: 'AR-DV1',
          role: 'receiver',
          level_unit: 's_meter_count',
          port: 'sim://sse-receiver',
          settings: { adapter: 'ardv1-sim', frequency_hz: 100000000, mode: 'FM' },
        },
      ],
    },
  });
  expect(sessionResponse.ok()).toBe(true);
  const session = (await sessionResponse.json()).data;
  const projectResponse = await request.post(base + '/monitoring-projects', {
    data: { name: 'Audio SSE regression', site: 'Synthetic only', session_id: session.id },
  });
  expect(projectResponse.ok()).toBe(true);
  let sourceReads = 0;
  page.on('request', (r) => {
    if (/\/sessions\/[^/]+\/(sweeps|observations)\?/.test(r.url())) sourceReads++;
  });
  const subscribed = page.waitForRequest((r) => new URL(r.url()).pathname === '/api/v1/events');
  await openDesk(page);
  await page.getByLabel('Project', { exact: true }).selectOption({ label: 'Audio SSE regression' });
  await subscribed;
  const interval = {
    started_at: '2026-11-11T12:00:00.000000Z',
    ended_at: '2026-11-11T12:00:00.100000Z',
  };
  const source = (id: string, level: number) => ({
    id,
    stream_id: session.streams[0].id,
    interval,
    frequencies_hz: [100000000, 100000001],
    levels: [level, level],
    level_unit: 'dBm',
    settings: { synthetic: true },
  });
  expect(
    (
      await request.post(base + '/sessions/' + session.id + '/sweeps', {
        data: source('sse-before-audio', -100),
      })
    ).ok(),
  ).toBe(true);
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
    'data-sweep-id',
    'sse-before-audio',
  );
  await expect(page.getByTestId('live-state')).toContainText('Live');
  const readsBeforeAudio = sourceReads;
  const wav = Buffer.alloc(44 + 1600);
  wav.write('RIFF', 0);
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
  const audio = await request.post(base + '/sessions/' + session.id + '/audio-segments', {
    data: {
      id: 'sse-synthetic-audio',
      stream_id: session.streams[1].id,
      frequency_hz: 100000000,
      mode: 'FM',
      interval,
      data_base64: wav.toString('base64'),
      sha256: createHash('sha256').update(wav).digest('hex'),
    },
  });
  expect(audio.ok(), await audio.text()).toBe(true);
  expect(
    (
      await request.post(base + '/sessions/' + session.id + '/sweeps', {
        data: source('sse-after-audio', -60),
      })
    ).ok(),
  ).toBe(true);
  await expect(page.locator('canvas.spectrum').first()).toHaveAttribute(
    'data-sweep-id',
    'sse-after-audio',
  );
  await expect(
    page.getByRole('img', { name: 'Waterfall, 2 committed rows, newest at top' }),
  ).toBeVisible();
  expect(sourceReads).toBe(readsBeforeAudio);
  await expect(page.getByText(/client sequence\/backlog gap/)).toHaveCount(0);
  const pixels = await page.locator('.waterfall canvas:visible').evaluate((canvas) => {
    const context = (canvas as HTMLCanvasElement).getContext('2d')!;
    return [
      Array.from(context.getImageData(0, 0, 1, 1).data),
      Array.from(context.getImageData(0, 1, 1, 1).data),
    ];
  });
  expect(pixels[0][3]).toBe(255);
  expect(pixels[1][3]).toBe(255);
  expect(pixels[0]).not.toEqual(pixels[1]);
});
