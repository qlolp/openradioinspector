"""Run actual platform polling against a hanging archive-capacity request."""

import shutil
import subprocess
import unittest
from pathlib import Path


class ArchiveCapacityPollingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for WebUI behavior tests")
    def test_platform_polling_and_sse_do_not_extend_capacity_timeout(self):
        script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const capacityCode = fs.readFileSync(process.argv[1], "utf8");
const platformCode = fs.readFileSync(process.argv[2], "utf8");
// Execute the production refresh and scheduleRefresh functions, rather than a
// stand-in that might forget to call the capacity poll on every platform refresh.
const start = platformCode.indexOf("  async function refresh(options = {}) {");
const end = platformCode.indexOf("  function setSseStatus(", start);
assert(start >= 0 && end > start);
const refreshCode = platformCode.slice(start, end);
const polling = platformCode.match(/window\.setInterval\(\(\) => refresh\(\{ silent: true \}\), 5000\);/)[0];
const flush = () => new Promise(resolve => setImmediate(resolve));

async function run(withSse) {
  let now = 0, nextId = 0;
  const timers = new Map(), requests = [];
  const status = { textContent: "", dataset: {} };
  function setTimer(callback, delay, repeat = false) {
    const id = ++nextId;
    timers.set(id, { callback, due: now + delay, delay, repeat });
    return id;
  }
  const context = vm.createContext({
    AbortController,
    state: { refreshRunning: false, refreshQueued: false, refreshTimer: null },
    dom: { health: {} },
    renderSession() {}, renderAll() {}, reportError(error) { throw error; },
    async performRefresh() {},
    document: { getElementById(id) { assert.equal(id, "archive-capacity-status"); return status; } },
    window: {
      setTimeout(fn, delay) { return setTimer(fn, delay); },
      clearTimeout(id) { timers.delete(id); },
      setInterval(fn, delay) { return setTimer(fn, delay, true); },
      clearInterval(id) { timers.delete(id); },
      addEventListener() {},
    },
    fetch(path, options) {
      assert.equal(path, "/api/v1/archive/capacity");
      return new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => {
          reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
        });
        requests.push({ resolve, options, started: now });
      });
    },
  });
  vm.runInContext(capacityCode, context);
  vm.runInContext(refreshCode, context);
  const payload = (state) => ({
    contract: "openradioinspector-platform", contract_version: 1,
    data: { storage_mode: "sqlite", database_bytes: 4096, max_database_bytes: 8192,
      remaining_bytes: 4096, state, warning_threshold: .9,
      measurement_writes_blocked: state === "full", automatic_deletion: false },
  });
  function respond(request, state) {
    request.resolve({ ok: true, status: 200, json: async () => payload(state) });
  }
  async function advance(duration) {
    const target = now + duration;
    while (true) {
      const next = [...timers.entries()].filter(([, task]) => task.due <= target)
        .sort((a, b) => a[1].due - b[1].due || a[0] - b[0])[0];
      if (!next) break;
      const [id, task] = next;
      now = task.due;
      if (task.repeat) task.due += task.delay;
      else timers.delete(id);
      task.callback();
      await flush();
    }
    now = target;
    await flush();
  }
  await context.refresh();
  respond(requests[0], "ok");
  await flush();
  assert.equal(status.dataset.state, "ok");
  await context.refresh(); // This request now hangs, while platform polling continues.
  vm.runInContext(polling, context);
  if (withSse) {
    context.window.setInterval(() => context.scheduleRefresh(0), 1500);
  }
  await advance(5000);
  assert.equal(requests.length, 2, "polling/SSE must reuse the in-flight request");
  assert.equal(requests[1].options.signal.aborted, false);
  await advance(1000);
  assert.equal(requests[1].options.signal.aborted, true, "original 6-second deadline survives polling");
  assert.equal(status.dataset.state, "unavailable");
  assert.match(status.textContent, /таймаут/);
  await advance(54000);
  assert.equal(status.dataset.state, "unavailable", "old ok must not survive 60 seconds of hanging polls");
  assert(requests.length <= 12, "coalescing bounds requests during frequent SSE refreshes");
  const recovered = context.window.OpenRadioArchiveCapacity.refresh();
  respond(requests.at(-1), "full");
  await recovered;
  assert.equal(status.dataset.state, "full");
}
(async () => { await run(false); await run(true); })()
  .then(() => console.log("behavior checks completed"))
  .catch(error => { console.error(error); process.exitCode = 1; });
'''
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        result = subprocess.run(
            [shutil.which("node"), "-e", script,
             str(static / "archive-capacity.js"), str(static / "platform.js")],
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("behavior checks completed", result.stdout, "Node exited before async checks completed")


if __name__ == "__main__":
    unittest.main()
