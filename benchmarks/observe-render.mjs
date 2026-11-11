// Optional passive browser observer. The host must mark data-sweep-id AFTER draw.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';

const options = {};
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i];
  if (!key?.startsWith('--') || process.argv[i + 1] === undefined) throw new Error('Expected named option/value pairs');
  options[key.slice(2)] = process.argv[i + 1];
}
const url = new URL(options.url);
const runId = options['run-id'];
const clients = Number(options.clients ?? 3);
const seconds = Number(options['duration-seconds'] ?? 1800);
if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
    !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(runId ?? '') ||
    !Number.isInteger(clients) || clients < 1 || clients > 8 || !Number.isFinite(seconds) || seconds <= 0 || seconds > 86400 ||
    !options.output) throw new Error('Invalid bounded observer configuration');

const output = path.resolve(options.output);
for (let current = path.dirname(output); ; current = path.dirname(current)) {
  const marker = path.join(current, '.git');
  if (fs.existsSync(marker) && (fs.statSync(marker).isFile() || fs.existsSync(path.join(marker, 'HEAD'))))
    throw new Error('Observer artifacts must be outside Git worktrees');
  if (path.dirname(current) === current) break;
}
fs.mkdirSync(path.dirname(output), { recursive: true });
const descriptor = fs.openSync(output, 'wx', 0o600);
const maxBytes = 64 * 1024 * 1024;
let written = 0; let records = 0; let dropped = 0; let browser; const pages = [];
function write(value, final = false) {
  const raw = Buffer.from(JSON.stringify(value) + '\n');
  if (!final && written + raw.length > maxBytes - 4096) { dropped++; return; }
  fs.writeSync(descriptor, raw); written += raw.length;
}
write({ kind: 'observer_metadata', version: 1, clients, clock: 'unix-wall-ns',
  host_sha256: crypto.createHash('sha256').update(os.hostname()).digest('hex'),
  draw_contract: 'data-sweep-id-after-draw' });
let status = 0;
try {
  const moduleName = process.env.ORI_PLAYWRIGHT_MODULE;
  const playwright = await import(moduleName ? pathToFileURL(path.resolve(moduleName)).href : 'playwright');
  browser = await playwright.chromium.launch({ headless: true,
    ...(process.env.ORI_BROWSER_EXECUTABLE ? { executablePath: process.env.ORI_BROWSER_EXECUTABLE } : {}),
    args: ['--disable-dev-shm-usage', '--no-sandbox', '--disable-background-timer-throttling',
      '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows'] });
  for (let client = 0; client < clients; client++) {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    pages.push(page);
    await page.exposeBinding('__oriBenchAfterDraw', (_source, batch) => {
      if (!Array.isArray(batch) || batch.length > 65) { dropped++; return; }
      for (const value of batch) {
        if (value.drop) { dropped += Number(value.drop); continue; }
        if (typeof value.id !== 'string' || !value.id.startsWith(runId + '-sweep-') || value.id.length > 128 ||
            !/^\d{1,20}$/.test(value.rendered_wall_ns)) { dropped++; continue; }
        const before = written;
        write({ kind: 'render', id: value.id, client: String(client), rendered_wall_ns: value.rendered_wall_ns });
        if (written > before) records++;
      }
    });
    await page.addInitScript(({ prefix }) => {
      const previous = new WeakMap(); let pending; let locallyDropped = 0; const queued = [];
      const flush = async () => {
        if (pending) { await pending; return flush(); }
        if (!queued.length && !locallyDropped) return;
        const batch = queued.splice(0, 64);
        if (locallyDropped) { batch.push({ drop: locallyDropped }); locallyDropped = 0; }
        pending = window.__oriBenchAfterDraw(batch);
        try { await pending; } finally { pending = undefined; }
        if (queued.length || locallyDropped) await flush();
      };
      Object.defineProperty(window, '__oriBenchObserverFinish', { value: flush });
      const scan = (nodes) => {
        for (const node of nodes) {
          if (!(node instanceof Element)) continue;
          const candidates = node.matches('[data-sweep-id]') ? [node] : [];
          const walker = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT);
          let visits = 0;
          for (let element = walker.nextNode(); element; element = walker.nextNode()) {
            if (++visits > 4096) { locallyDropped++; break; }
            if (!element.matches('[data-sweep-id]')) continue;
            if (candidates.length === 64) { locallyDropped++; break; }
            candidates.push(element);
          }
          for (const element of candidates) {
            const id = element.getAttribute('data-sweep-id');
            if (!id?.startsWith(prefix) || previous.get(element) === id) continue;
            previous.set(element, id);
            if (queued.length === 64) { locallyDropped++; continue; }
            queued.push({ id, rendered_wall_ns: String(Math.round((performance.timeOrigin + performance.now()) * 1e6)) });
          }
        }
        if (!pending) void flush();
      };
      const observer = new MutationObserver((records) => {
        const nodes = [];
        for (const record of records) {
          if (record.type === 'attributes') {
            if (nodes.length === 64) locallyDropped++; else nodes.push(record.target);
          } else {
            for (const node of record.addedNodes) {
              if (nodes.length === 64) locallyDropped++; else nodes.push(node);
            }
          }
        }
        scan(nodes);
      });
      observer.observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-sweep-id'] });
    }, { prefix: runId + '-sweep-' });
    await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: 30000 });
  }
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, seconds * 1000);
    const stop = () => { clearTimeout(timer); resolve(); };
    process.once('SIGINT', stop); process.once('SIGTERM', stop);
  });
  for (const page of pages) await page.evaluate(async () => { await window.__oriBenchObserverFinish?.(); });
} catch {
  status = 1; dropped++;
  process.stderr.write('Observer failed; no UI latency acceptance claim was made\n');
} finally {
  if (browser) await browser.close();
  write({ kind: 'observer_summary', records, dropped, failed: status !== 0 }, true);
  fs.fsyncSync(descriptor); fs.closeSync(descriptor);
}
process.exitCode = status;
