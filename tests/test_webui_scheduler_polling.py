"""Fake-clock execution of delayed, hung and interrupted scheduler polling."""

import shutil
import subprocess
import unittest
from pathlib import Path


class SchedulerPollingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for WebUI behavior tests")
    def test_delayed_reads_timeout_independence_recovery_and_safe_actions(self):
        script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));
const task = { name: "pending/task", status: "В очереди на выполнение" };
const statusData = (enabled = true) => ({ enabled, tasks: { total: 1 }, runtime: { last_result: "idle" } });
const tasksData = (items = [task]) => ({ items, count: items.length });
function element() {
  return { textContent: "", dataset: {}, children: [], listeners: {}, disabled: false,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, fn) { this.listeners[name] = fn; } };
}
function setup(initialBehavior) {
  let now = 0, nextId = 0, behavior = initialBehavior;
  const timers = new Map(), ids = {}, requests = [], events = {};
  function schedule(callback, delay, repeat = false) {
    const id = ++nextId;
    timers.set(id, { callback, delay, repeat, due: now + delay });
    return id;
  }
  const context = {
    AbortController,
    document: { getElementById(id) { return ids[id] ||= element(); }, createElement: element },
    window: {
      setTimeout: schedule, clearTimeout(id) { timers.delete(id); },
      setInterval(fn, ms) { return schedule(fn, ms, true); }, clearInterval(id) { timers.delete(id); },
      addEventListener(name, fn) { events[name] = fn; },
    },
    fetch(path, options) {
      return new Promise((resolve, reject) => {
        const request = { path, options, resolve, reject, started: now };
        requests.push(request);
        // Deliberately allow late responses after abort: generation checks and
        // the deadline must still prevent a stale read from being applied.
        behavior(request, reply, schedule);
      });
    },
  };
  function reply(request, data) {
    request.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ data }) });
  }
  vm.runInNewContext(source, context);
  return {
    requests, events, ids, reply, setBehavior(fn) { behavior = fn; },
    async advance(duration) {
      const end = now + duration;
      while (true) {
        const next = [...timers.entries()].filter(([, t]) => t.due <= end)
          .sort((a, b) => a[1].due - b[1].due || a[0] - b[0])[0];
        if (!next) break;
        const [id, t] = next;
        now = t.due;
        if (t.repeat) t.due += t.delay;
        else timers.delete(id);
        t.callback();
        await flush();
      }
      now = end;
      await flush();
    },
  };
}
function normal(request, reply) {
  reply(request, request.path.endsWith("/tasks") ? tasksData() : statusData());
}
(async () => {
  // Every read takes longer than the 2-second poll, but still must be shown.
  const slow = setup((request, reply, schedule) => schedule(() => normal(request, reply), 2500));
  await slow.advance(2000);
  assert.equal(slow.requests.length, 2, "in-flight reads coalesce at the next poll");
  await slow.advance(500);
  assert.equal(slow.ids["task-scheduler-status"].dataset.enabled, "true");
  assert.equal(slow.ids["task-scheduler-list"].children[0].children[0].textContent, task.name);
  await slow.advance(9500);
  assert.equal(slow.requests.length, 8, "reads do not accumulate across delayed polls");

  // Either endpoint may hang. The other keeps rendering and polling; the hung
  // one expires at its original deadline, even after three interval callbacks.
  for (const hanging of ["status", "tasks"]) {
    const app = setup((request, reply) => {
      const key = request.path.endsWith("/tasks") ? "tasks" : "status";
      if (key !== hanging) normal(request, reply);
    });
    await flush();
    const status = app.ids["task-scheduler-status"], list = app.ids["task-scheduler-list"];
    if (hanging === "tasks") assert.equal(status.dataset.enabled, "true");
    else assert.equal(list.children[0].children[0].textContent, task.name);
    const old = app.requests.find(r => r.path.endsWith("/tasks") === (hanging === "tasks"));
    await app.advance(6000);
    assert.equal(old.options.signal.aborted, true);
    const errorText = hanging === "status" ? status.textContent : list.children[0].textContent;
    assert.match(errorText, /Таймаут/);
    assert.equal(app.requests.filter(r => r.path === old.path).length, 1);
    assert.equal(app.requests.filter(r => r.path !== old.path).length, 4);
    app.setBehavior(normal);
    await app.advance(2000);
    assert.equal(status.dataset.enabled, "true");
    assert.equal(list.children[0].children[0].textContent, task.name);
    app.reply(old, hanging === "status" ? statusData(false) : tasksData([]));
    await flush();
    assert.equal(status.dataset.enabled, "true", "late timed-out status cannot overwrite recovery");
    assert.equal(list.children[0].children[0].textContent, task.name);
  }

  // The deadline covers a hanging response body, not just response headers.
  const body = setup((request) => request.resolve({ ok: true, status: 200,
    text: () => new Promise(() => {}) }));
  await flush();
  await body.advance(6000);
  assert.match(body.ids["task-scheduler-status"].textContent, /Таймаут/);
  assert.match(body.ids["task-scheduler-list"].children[0].textContent, /Таймаут/);

  // Hide/show during an uncertain command cannot resubmit it or accept a stale
  // response. Reads resume when that command settles and show server state.
  const action = setup(normal);
  await flush();
  action.setBehavior(() => {});
  await action.advance(2000);
  const oldReads = action.requests.slice(-2);
  action.ids["task-scheduler-disable"].listeners.click();
  const post = action.requests.at(-1), count = action.requests.length;
  assert.equal(post.options.method, "POST");
  assert.equal(post.options.signal, undefined);
  assert(oldReads.every(r => r.options.signal.aborted));
  action.events.pagehide(); action.events.pageshow();
  action.ids["task-scheduler-disable"].listeners.click();
  await action.advance(10000);
  assert.equal(action.requests.length, count, "no timeout or automatic retry of uncertain POST");
  assert.equal(action.ids["task-scheduler-disable"].disabled, true);
  action.setBehavior(normal);
  action.reply(post, statusData(false));
  await flush();
  assert.equal(action.ids["task-scheduler-status"].dataset.enabled, "true", "fresh read, not pre-hide POST result");
  oldReads.forEach(request => action.reply(request, request.path.endsWith("/tasks") ? tasksData([]) : statusData(false)));
  await flush();
  assert.equal(action.ids["task-scheduler-status"].dataset.enabled, "true");
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
