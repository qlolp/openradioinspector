import { expect, test } from '@playwright/test';
import { writeFile } from 'node:fs/promises';

test('replayed A then B replaces a provisional future-B owner without a stale final A marker', async ({
  page,
}, info) => {
  await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
  await page.goto('./e2e/live-publication.html', { waitUntil: 'networkidle' });
  await page.waitForFunction(
    () => typeof (window as any).runLivePublicationDiagnostic === 'function',
  );
  const result = await page.evaluate(() =>
    (window as any).runLivePublicationDiagnostic('seed-replay'),
  );
  let closed: any;
  try {
    expect(result.applied.map((row: any) => row.id)).toEqual(['committed-é-A', 'committed-é-B']);
    expect(result.applied.map((row: any) => row.cursor)).toEqual([
      '9007199254740993',
      '9007199254740994',
    ]);
    expect(result.seedDraw.marker).toBeNull();
    expect(result.seedDraw.pixels).not.toBeNull();
    expect(result.seedDraw.sourceFooter).toBe('committed-é-B');
    expect(result.heldParserItems).toBe(1);
    expect(result.maximumPending).toBe(1);
    expect(result.final.sourceFooter).toBe('committed-é-B');
    expect(result.final.retainedRowsLabel).toContain('2 committed rows');
    expect(result.capture.dropped).toBe(0);
    expect(result.parsedNegativeZero).toBe(true);
    expect(result.parsedArraysUnchanged).toBe(true);
    expect(result.inputArraysUnchanged).toBe(true);
    // Same five-second final-marker oracle as the unchanged scope-replay case.
    // The fixture has already let queued native draw/paint callbacks drain.
    await expect(page.locator('.live-grid canvas.spectrum').first()).toHaveAttribute(
      'data-sweep-id',
      'committed-é-B',
      { timeout: 5000 },
    );
  } finally {
    closed = await page.evaluate(() => (window as any).__closeLiveSeedOrderForTest());
    const proof = { ...result, afterOracle: closed };
    await writeFile(info.outputPath('live-seed-order.json'), JSON.stringify(proof, null, 2));
    await info.attach('live-seed-order.json', {
      body: JSON.stringify(proof, null, 2),
      contentType: 'application/json',
    });
    expect(closed.capture.dropped).toBe(0);
    expect(closed.cleanup).toEqual({
      subscriptionClosed: true,
      pending: 0,
      allParsersTerminated: true,
    });
  }
});
