import { expect, test } from '@playwright/test';
import { writeFile } from 'node:fs/promises';

for (const mode of ['coalesced', 'spaced'] as const) {
  test(`real App retains every applied source marker with ${mode} publication`, async ({
    page,
  }, info) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(String(error)));
    await page.addInitScript(() => localStorage.setItem('ori-modern-locale', 'en'));
    await page.goto('./e2e/live-publication.html', { waitUntil: 'networkidle' });
    await page.waitForFunction(
      () => typeof (window as any).runLivePublicationDiagnostic === 'function',
    );
    const result = await page.evaluate(
      (kind) => (window as any).runLivePublicationDiagnostic(kind),
      mode,
    );
    await writeFile(
      info.outputPath('live-publication.json'),
      JSON.stringify({ ...result, pageErrors: errors }, null, 2),
    );
    await info.attach('live-publication.json', {
      body: JSON.stringify(result, null, 2),
      contentType: 'application/json',
    });
    expect(errors).toEqual([]);
    expect(result.maximumPending).toBe(1);
    expect(result.applied.map((row: any) => row.cursor)).toEqual([
      '9007199254740993',
      '9007199254740994',
      '9007199254740995',
    ]);
    expect(result.parsed.map((row: any) => row.id)).toEqual([
      'committed-é-A',
      'committed-é-B',
      'committed-é-C',
    ]);
    expect(result.final.retainedRowsLabel).toContain('3 committed rows');
    expect(result.final.sourceFooter).toBe('committed-é-C');
    expect(result.parsedNegativeZero).toBe(true);
    expect(result.parsedArraysUnchanged).toBe(true);
    expect(result.inputArraysUnchanged).toBe(true);
    expect(result.subscriptionClosed).toBe(true);
    if (mode === 'coalesced') {
      expect(new Set(result.applied.map((row: any) => row.frame)).size).toBe(1);
      expect(result.afterApplied.retainedRowsLabel).toContain('0 committed rows');
      expect(result.afterApplied.marker).toBeNull();
    }
    expect(result.observed.map((row: any) => row.id)).toEqual([
      'committed-é-A',
      'committed-é-B',
      'committed-é-C',
    ]);
    for (const row of result.observed) {
      expect(row.marker).toBe(row.id);
      expect(row.pixels).not.toBeNull();
      expect(row.label).toContain('raw S-meter');
      expect(row.unit).toBe('raw S-meter');
    }
  });
}
