"""Execute scheduler polling to catch missing functions and stale UI updates."""

import shutil
import subprocess
import unittest
from pathlib import Path


class SchedulerJavascriptBehaviorTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for WebUI behavior tests")
    def test_polling_isolation_actions_and_page_lifecycle(self):
        script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
function element() {
  return { textContent: "", dataset: {}, children: [], listeners: {}, disabled: false,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, fn) { this.listeners[name] = fn; } };
}
const ids = {}, events = {}, requests = [];
let poll, cleared = 0;
const context = {
  AbortController,
  document: { getElementById(id) { return ids[id] ||= element(); }, createElement: element },
  window: {
    setInterval(fn) { poll = fn; return 1; }, clearInterval() { cleared++; },
    setTimeout() { return 2; }, clearTimeout() {},
    addEventListener(name, fn) { events[name] = fn; },
  },
  fetch(path, options) { return new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })); },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const status = ids["task-scheduler-status"], list = ids["task-scheduler-list"], enable = ids["task-scheduler-enable"];
const flush = () => new Promise(resolve => setImmediate(resolve));
const state = (enabled = false) => ({ enabled, tasks: { total: 1, running: 0, completed: 0 }, runtime: { last_result: "idle" } });
const task = { name: "task/slash", status: "В очереди на выполнение" };
function respond(request, data, code = 200) {
  request.resolve({ ok: code < 400, status: code, text: async () => JSON.stringify({ data }) });
}
function respondPair(pair, enabled = false, items = [task]) {
  respond(pair[0], state(enabled)); respond(pair[1], { items, count: items.length });
}
(async () => {
  assert.deepEqual(requests.map(r => r.path), ["/api/v1/task-scheduler", "/api/v1/task-scheduler/tasks"]);
  respondPair(requests);
  await flush();
  assert.equal(status.dataset.enabled, "false");
  assert.equal(enable.disabled, false);
  assert.equal(list.children[0].children[0].textContent, "task/slash");

  let pending = poll(), pair = requests.slice(-2);
  respond(pair[0], state(true)); respond(pair[1], {}, 404);
  await pending;
  assert.equal(status.dataset.enabled, "true");
  assert.match(list.children[0].textContent, /Список ControlTask недоступен/);
  pending = poll(); pair = requests.slice(-2);
  respond(pair[0], {}, 503); respond(pair[1], { items: [task], count: 1 });
  await pending;
  assert.match(status.textContent, /недоступно/);
  assert.equal(enable.disabled, true);
  assert.equal(list.children[0].children[0].textContent, "task/slash");

  const old = poll(), oldPair = requests.slice(-2), beforeRefresh = requests.length;
  const fresh = poll();
  assert.equal(requests.length, beforeRefresh, "Slow polls must coalesce");
  respondPair(requests.slice(-2), true);
  await fresh;
  respondPair(oldPair, false);
  await old;
  assert.equal(status.dataset.enabled, "true");

  pending = poll(); respondPair(requests.slice(-2)); await pending;
  const stale = poll(), stalePair = requests.slice(-2);
  enable.listeners.click();
  const post = requests.at(-1), afterPost = requests.length;
  assert.equal(post.options.method, "POST");
  assert.equal(post.options.signal, undefined, "POST is not aborted or automatically retried");
  assert.deepEqual(JSON.parse(post.options.body), { enabled: true });
  enable.listeners.click();
  await poll();
  assert.equal(requests.length, afterPost, "No duplicate action or poll during POST");
  respondPair(stalePair, false);
  await stale;
  respond(post, state(true));
  await flush();
  assert.equal(status.dataset.enabled, "true");

  // A queued task is cancelled through its encoded name and followed by fresh reads.
  const cancel = list.children[0].children.at(-1);
  cancel.listeners.click();
  const cancelPost = requests.at(-1);
  assert.equal(cancelPost.path, "/api/v1/task-scheduler/tasks/task%2Fslash/cancel");
  assert.equal(cancelPost.options.method, "POST");
  respond(cancelPost, {});
  await flush();
  respondPair(requests.slice(-2), true, []);
  await flush();
  assert.match(list.children[0].textContent, /нет задач/);

  const hidden = poll(), hiddenPair = requests.slice(-2);
  events.pagehide();
  respondPair(hiddenPair, false);
  await hidden;
  assert.equal(status.dataset.enabled, "true");
  assert.equal(cleared, 1);
  events.pageshow();
  respondPair(requests.slice(-2), false);
  await flush();
  assert.equal(status.dataset.enabled, "false");

  pending = poll(); pair = requests.slice(-2);
  respond(pair[0], {}); respond(pair[1], { items: [null], count: 1 });
  await pending;
  assert.match(status.textContent, /Некорректный статус/);
  assert.match(list.children[0].textContent, /Некорректный список/);
})().then(() => console.log("behavior checks completed"))
  .catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script,
             str(Path(__file__).resolve().parents[1] / "radioinspector/web/static/scheduler.js")],
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("behavior checks completed", result.stdout, "Node exited before async checks completed")


if __name__ == "__main__":
    unittest.main()
