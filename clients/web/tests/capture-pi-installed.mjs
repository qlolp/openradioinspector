// Browser capture inside packaging/pi/test-installed.sh's disposable network namespace.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { chromium, expect } from '@playwright/test';

const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
const fixture = JSON.parse(await readFile(new URL('../../../contracts/fixtures/measurement-v1.json', import.meta.url), 'utf8'));
const viewport = { width: 1366, height: 768 };

export async function captureFixture(browser, origin) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1, locale: 'ru-RU' });
  try {
    async function api(route, body) {
      const url = origin + '/api/v1' + route;
      const response = body === undefined ? await page.request.get(url) : await page.request.post(url, { data: body });
      try {
        assert.equal(response.ok(), true, `${route} returned ${response.status()}`);
        return (await response.json()).data;
      } finally {
        await response.dispose();
      }
    }

    const sessionPath = '/sessions/' + encodeURIComponent(fixture.session.id);
    assert.equal((await api(sessionPath)).name, fixture.session.name);
    const existing = await api(sessionPath + '/sweeps?limit=2');
    assert.deepEqual(existing.map((row) => row.id), [fixture.sweep.id]);
    assert.equal((await api('/monitoring-projects?limit=1')).items.length, 0);
    assert.equal((await api('/workers')).some((worker) => worker.alive), false);
    const project = await api('/monitoring-projects', {
      name: 'Учебная станция · синтетический спектр',
      site: 'Синтетические измерения установочного теста',
      session_id: fixture.session.id,
    });
    assert.equal(project.active_session_id, fixture.session.id);
    assert.equal(project.analyzer_stream_id, fixture.sweep.stream_id);
    assert.equal(project.state, 'draft');
    assert.equal(project.autostart, false);
    const guard = '/monitoring-projects/' + encodeURIComponent(project.id) + '/status';
    assert.equal((await api(guard)).active, false);

    await page.addInitScript((id) => {
      localStorage.setItem('ori-modern-project', id);
      localStorage.setItem('ori-modern-locale', 'ru');
    }, project.id);
    await page.goto(origin + '/modern/', { waitUntil: 'domcontentloaded' });
    const consent = page.getByTestId('legacy-compatibility-consent');
    const workspace = page.getByTestId('workspace-heading');
    await consent.or(workspace).first().waitFor();
    if (await consent.isVisible()) {
      await consent.check();
      await page.getByTestId('legacy-compatibility-continue').click();
    }
    await expect(workspace).toBeVisible();
    await page.getByTestId('project-select').selectOption(project.id);
    const spectrum = page.locator('canvas.spectrum').first();
    await expect(spectrum).toHaveAttribute('data-sweep-id', fixture.sweep.id);
    const count = 96;
    const now = Date.now();
    const frequencies = Array.from({ length: 201 }, (_, bin) => 88_000_000 + bin * 100_000);
    let lastId;
    for (let start = 0; start < count; start += 12) {
      const items = Array.from({ length: 12 }, (_, offset) => {
        const index = start + offset;
        const at = now - (count - index) * 4000;
        const carrier = index < count / 2 ? 44 : 156;
        return {
          ...fixture.sweep,
          id: 'pi-guide-synthetic-' + String(index).padStart(3, '0'),
          interval: { started_at: new Date(at).toISOString(), ended_at: new Date(at + 1000).toISOString() },
          frequencies_hz: frequencies,
          levels: frequencies.map((_, bin) => Math.max(-107 + ((bin * 7 + index * index) % 13),
            -32 - Math.abs(bin - carrier) * 6, -70 - Math.abs(bin - 114) * 5)),
          settings: { ...fixture.sweep.settings, synthetic: true, generator_output: false },
        };
      });
      const committed = await api(sessionPath + '/sweeps/batch', { items });
      assert.deepEqual(committed.items.map((row) => row.id), items.map((row) => row.id));
      lastId = items.at(-1).id;
      await expect(spectrum).toHaveAttribute('data-sweep-id', lastId, { timeout: 15000 });
    }
    const waterfall = page.locator('.waterfall');
    await expect.poll(async () => Number((await waterfall.getAttribute('aria-label')).match(/\d+/)?.[0])).toBeGreaterThanOrEqual(count);
    assert.equal((await api(sessionPath + '/sweeps?limit=1'))[0].id, lastId);
    assert.equal((await api(guard)).active, false);
    assert.equal((await api('/workers')).some((worker) => worker.alive), false);
    const png = await page.screenshot();
    return { png, evidence: { url: page.url(), viewport, device_scale_factor: 1, synthetic: true,
      project_id: project.id, session_id: fixture.session.id, committed_sweeps_added: count,
      displayed_original_sweep_id: lastId, waterfall: await waterfall.getAttribute('aria-label'),
      chromium: browser.version(), capture_sha256: sha256(png) } };
  } finally {
    await page.close();
  }
}

if (import.meta.main) {
  const [artifact, output] = process.argv.slice(2);
  assert.equal(process.platform, 'linux');
  assert.equal(process.arch, 'arm64');
  assert.equal((await stat('/proc/self/ns/net')).ino, (await stat('/run/netns/ori-pi-rc-ci')).ino);
  const manifest = JSON.parse(await readFile('/usr/share/doc/openradioinspector-pi-rc/release.json', 'utf8'));
  const installed = execFileSync('dpkg-query', ['-W', '-f=${Version}', 'openradioinspector-pi-rc'], { encoding: 'utf8' });
  assert.equal(installed, manifest.debian_version);
  assert.equal(installed, execFileSync('dpkg-deb', ['-f', artifact, 'Version'], { encoding: 'utf8' }).trim());
  const browser = await chromium.launch({ args: ['--use-angle=swiftshader-webgl', '--enable-unsafe-swiftshader'] });
  try {
    const { png, evidence } = await captureFixture(browser, 'http://127.0.0.1:8770');
    const receipt = { ...evidence, host: 'GitHub-hosted ARM64 Linux VM', hardware_tested: false, physical_pi: false,
      os_release: (await readFile('/etc/os-release', 'utf8')).split('\n').find((line) => line.startsWith('PRETTY_NAME=')),
      kernel: os.release(), architecture: process.arch, source_revision: manifest.source_revision,
      release_version: manifest.release_version, debian_version: installed, package: path.basename(artifact),
      package_sha256: sha256(await readFile(artifact)), embedded_index_sha256: manifest.web_assets['/modern/index.html'],
      image: 'pi-monitoring.png' };
    await writeFile(path.join(output, receipt.image), png, { flag: 'wx' });
    await writeFile(path.join(output, 'pi-monitoring.json'), JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx' });
  } finally {
    await browser.close();
  }
}
