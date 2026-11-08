"""Capacity client behavior, including errors, reconnects and late replies."""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock

from radioinspector.web.server import create_server
from radioinspector.windows.archive_capacity import (
    CAPACITY_NOTE, archive_capacity_color, archive_capacity_text,
)
from radioinspector.windows.app import WindowsApp
from radioinspector.windows.client import PlatformClient, PlatformClientError, PlatformSnapshot


ROOT = Path(__file__).resolve().parents[1]


def capacity(state="ok", **changes):
    result = {
        "storage_mode": "sqlite", "database_bytes": 4096,
        "max_database_bytes": 8192, "remaining_bytes": 4096,
        "state": state, "warning_threshold": 0.9,
        "measurement_writes_blocked": state == "full", "automatic_deletion": False,
    }
    result.update(changes)
    return result


class WindowsCapacityTests(unittest.TestCase):
    def test_presentations_preserve_exact_bytes_and_report_all_states(self):
        self.assertIn("4 096 Б / 8 192 Б", archive_capacity_text(capacity()))
        self.assertIn("осталось 4 096 Б", archive_capacity_text(capacity()))
        self.assertIn("≥ 90%", archive_capacity_text(capacity("warning")))
        self.assertIn("заблокирована", archive_capacity_text(capacity("full")))
        self.assertEqual(archive_capacity_color(capacity("full")), "#b3261e")
        self.assertEqual(archive_capacity_color(capacity("warning")), "#8a5700")
        self.assertIn("без лимита", archive_capacity_text(capacity(
            "unlimited", max_database_bytes=None, remaining_bytes=None
        )))
        memory = capacity("unavailable", storage_mode="memory", database_bytes=None,
                          max_database_bytes=None, remaining_bytes=None)
        self.assertIn("Архив в памяти", archive_capacity_text(memory))
        self.assertIn("ёмкость недоступна", archive_capacity_text(capacity("unavailable")))
        self.assertIn("недоступна (HTTP 404)", archive_capacity_text(capacity(), "HTTP 404"))
        self.assertIn("состояние не получено", archive_capacity_text(None))
        for detail in ("Автоматическое удаление", "WAL", "метаданных"):
            self.assertIn(detail, CAPACITY_NOTE)

    def test_invalid_payloads_are_typed_errors_not_zero_or_unlimited(self):
        client = PlatformClient("http://example.test")
        for bad in (None, [], {}, capacity(database_bytes=True), capacity(database_bytes=-1),
                    capacity(database_bytes="0"), capacity(state="unknown"), capacity(state=[]), capacity(state={}),
                    capacity(max_database_bytes=None), capacity(automatic_deletion=True)):
            with self.subTest(payload=bad):
                client._request = Mock(return_value=bad)
                with self.assertRaises(PlatformClientError) as caught:
                    client.archive_capacity()
                self.assertEqual(caught.exception.code, "invalid_archive_capacity")
                client._request.assert_called_once_with("GET", "/archive/capacity")

    def test_snapshot_isolates_old_server_and_recovers_with_or_without_session(self):
        client = PlatformClient("http://example.test")
        session = {"id": "session", "streams": []}
        calls = []
        answer = None

        def request(method, path, payload=None):
            calls.append((method, path))
            if path == "/archive/capacity":
                if answer is None:
                    raise PlatformClientError("HTTP 404", status=404)
                return answer
            if path == "/sessions":
                return [session]
            if path == "/task-scheduler/tasks":
                return {"items": [], "count": 0}
            if path == "/sessions/session":
                return session
            if path.endswith(("/workers", "/candidates", "/investigations")):
                return []
            return {}

        client._request = request
        for session_id in (None, "session"):
            with self.subTest(session_id=session_id):
                answer = None
                snapshot = client.snapshot(session_id)
                self.assertIsNone(snapshot.archive_capacity)
                self.assertEqual(snapshot.archive_capacity_error, "HTTP 404")
                self.assertEqual(snapshot.sessions, [session])
                self.assertIsNone(snapshot.task_scheduler_error)
                answer = capacity("warning")
                recovered = client.snapshot(session_id)
                self.assertEqual(recovered.archive_capacity, answer)
                self.assertIsNone(recovered.archive_capacity_error)
        for malformed_state in ([], {}):
            for session_id in (None, "session"):
                with self.subTest(malformed_state=malformed_state, session_id=session_id):
                    answer = capacity(state=malformed_state)
                    snapshot = client.snapshot(session_id)
                    self.assertIsNone(snapshot.archive_capacity)
                    self.assertIn("Неизвестное состояние", snapshot.archive_capacity_error)
                    self.assertEqual(snapshot.sessions, [session])
                    self.assertIsNone(snapshot.task_scheduler_error)
                    self.assertIsNone(snapshot.task_scheduler_tasks_error)
        self.assertTrue(all(method == "GET" for method, _ in calls))
        self.assertFalse(any("sweeps" in path or "export" in path for _, path in calls))

    def test_tk_status_changes_and_old_generation_is_not_applied(self):
        app = WindowsApp.__new__(WindowsApp)
        app.archive_capacity_var = Mock()
        app.archive_capacity_label = Mock()
        app._show_archive_capacity(capacity("full"))
        self.assertIn("заблокирована", app.archive_capacity_var.set.call_args.args[0])
        app.archive_capacity_label.configure.assert_called_with(foreground="#b3261e")
        app._show_archive_capacity(None, "connection lost")
        self.assertNotIn("4 096", app.archive_capacity_var.set.call_args.args[0])
        app.archive_capacity_label.configure.assert_called_with(foreground="#666666")

        app.server_var = Mock()
        app.client_factory = Mock()
        app.connection_var = Mock()
        app._revision = 1
        app.refresh = Mock()
        # This capacity-only fixture deliberately does not build spectrum widgets.
        app._clear_spectrum = Mock()
        app.connect()
        app._clear_spectrum.assert_called_once()
        self.assertIn("состояние не получено", app.archive_capacity_var.set.call_args.args[0])
        self.assertEqual(app._revision, 2)

        app._closed = False
        app._revision = 2
        app._messages = queue.Queue()
        app.root = Mock()
        app._render_controls = Mock()
        app.refresh = Mock()
        app._apply_snapshot = Mock()
        app.connection_var = Mock()
        stale = Future()
        stale.set_result(PlatformSnapshot({}, {}, [], archive_capacity=capacity("full")))
        app._messages.put(("snapshot", stale, 1, ""))
        app._drain_messages()
        app._apply_snapshot.assert_not_called()
        app.refresh.assert_called_once()
        current = Future()
        current.set_result(PlatformSnapshot({}, {}, [], archive_capacity=capacity()))
        app._messages.put(("snapshot", current, 2, ""))
        app._drain_messages()
        app._apply_snapshot.assert_called_once_with(current.result())
        failure = Future()
        failure.set_exception(PlatformClientError("connection lost"))
        app._messages.put(("snapshot", failure, 2, ""))
        app._drain_messages()
        self.assertIn("недоступна", app.archive_capacity_var.set.call_args.args[0])

    def test_real_http_capacity_is_global_and_snapshot_does_not_need_session(self):
        server = create_server(host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            client = PlatformClient(f"http://{host}:{port}")
            value = client.archive_capacity()
            self.assertEqual(value["storage_mode"], "memory")
            self.assertEqual(value["state"], "unavailable")
            self.assertIsNone(value["database_bytes"])
            snapshot = client.snapshot()
            self.assertEqual(snapshot.archive_capacity, value)
            self.assertEqual(snapshot.sessions, [])
            self.assertIsNone(snapshot.archive_capacity_error)
        finally:
            server.automatic_detection.close()
            server.automatic_handoff.close()
            server.automatic_completion.close()
            server.control_scheduler.close()
            server.worker_manager.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            server.session_service.close()


class WebCapacityTests(unittest.TestCase):
    def test_panel_is_loaded_before_and_called_by_existing_refresh(self):
        html = (ROOT / "radioinspector/web/static/platform.html").read_text(encoding="utf-8")
        platform = (ROOT / "radioinspector/web/static/platform.js").read_text(encoding="utf-8")
        self.assertLess(html.index('src="/static/archive-capacity.js"'), html.index('src="/static/platform.js"'))
        self.assertIn('id="archive-capacity-status"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("window.OpenRadioArchiveCapacity?.refresh()", platform)
        for text in ("Автоматическое удаление", "WAL", "метаданных"):
            self.assertIn(text, html)

    @unittest.skipUnless(shutil.which("node"), "Node is required for WebUI behavior tests")
    def test_javascript_states_errors_recovery_and_stale_replies(self):
        script = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const element = { textContent: "", dataset: {} };
const requests = [], listeners = {}, timers = new Map();
let nextTimer = 0;
const window = {
  setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer; },
  clearTimeout(id) { timers.delete(id); },
  addEventListener(name, fn) { listeners[name] = fn; },
};
const context = {
  window, AbortController,
  document: { getElementById(id) { assert.equal(id, "archive-capacity-status"); return element; } },
  fetch(path, options) {
    assert.equal(path, "/api/v1/archive/capacity");
    assert.equal(options.method, "GET");
    return new Promise((resolve, reject) => requests.push({ resolve, reject, options }));
  },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const refresh = window.OpenRadioArchiveCapacity.refresh;
function data(state = "ok", overrides = {}) {
  return { storage_mode: "sqlite", database_bytes: 4096, max_database_bytes: 8192,
    remaining_bytes: 4096, state, warning_threshold: .9,
    measurement_writes_blocked: state === "full", automatic_deletion: false, ...overrides };
}
function respond(request, value, envelope = {}) {
  request.resolve({ ok: true, status: 200, json: async () => ({
    contract: "openradioinspector-platform", contract_version: 1, data: value, ...envelope,
  }) });
}
(async () => {
  for (const state of ["ok", "warning", "full", "unlimited", "unavailable"]) {
    const pending = refresh();
    respond(requests.at(-1), data(state, state === "unlimited" ? { max_database_bytes: null, remaining_bytes: null } : {}));
    await pending;
    assert.equal(element.dataset.state, state);
    assert.match(element.textContent, /4\s096 Б/);
    if (state === "warning") assert.match(element.textContent, /≥ 90%/);
    if (state === "full") assert.match(element.textContent, /заблокирована/);
    if (state === "unlimited") assert.match(element.textContent, /без лимита/);
  }
  let pending = refresh();
  respond(requests.at(-1), data("unavailable", { storage_mode: "memory", database_bytes: null,
    max_database_bytes: null, remaining_bytes: null }));
  await pending;
  assert.match(element.textContent, /Архив в памяти/);

  for (const bad of [null, {}, data("unknown"), data("ok", { database_bytes: "0" }),
      data("ok", { automatic_deletion: true }), data("ok", { max_database_bytes: null })]) {
    pending = refresh();
    respond(requests.at(-1), bad);
    await pending;
    assert.equal(element.dataset.state, "unavailable");
    assert.match(element.textContent, /недоступна/);
  }
  pending = refresh();
  respond(requests.at(-1), data(), { contract_version: 2 });
  await pending;
  assert.match(element.textContent, /контракт/);
  pending = refresh();
  requests.at(-1).resolve({ ok: false, status: 404 });
  await pending;
  assert.match(element.textContent, /HTTP 404/);
  pending = refresh();
  requests.at(-1).reject(new Error("<img src=x>"));
  await pending;
  assert.match(element.textContent, /<img src=x>/); // Literal text, never HTML.
  assert.match(element.textContent, /Остальные панели/);
  pending = refresh();
  requests.at(-1).resolve({ ok: true, status: 200, json: async () => { throw new Error("bad JSON"); } });
  await pending;
  assert.match(element.textContent, /bad JSON/);
  pending = refresh();
  requests.at(-1).reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
  await pending;
  assert.match(element.textContent, /таймаут/);

  const coalesced = refresh(), requestCount = requests.length;
  const alsoPending = refresh();
  assert.equal(requests.length, requestCount);
  respond(requests.at(-1), data());
  await Promise.all([coalesced, alsoPending]);
  const old = refresh(), oldRequest = requests.at(-1);
  listeners.pagehide();
  const fresh = refresh();
  assert.equal(oldRequest.options.signal.aborted, true);
  respond(requests.at(-1), data("warning"));
  await fresh;
  respond(oldRequest, data("full"));
  await old;
  assert.equal(element.dataset.state, "warning");
  const lateFailure = refresh(), failedRequest = requests.at(-1);
  listeners.pagehide();
  pending = refresh();
  respond(requests.at(-1), data());
  await pending;
  failedRequest.reject(new Error("late error"));
  await lateFailure;
  assert.equal(element.dataset.state, "ok");
  pending = refresh();
  const unloaded = requests.at(-1);
  listeners.pagehide();
  respond(unloaded, data("full"));
  await pending;
  assert.equal(element.dataset.state, "ok");
  assert.equal(timers.size, 0);
  pending = refresh();
  respond(requests.at(-1), data("full"));
  await pending;
  assert.equal(element.dataset.state, "full");
})().then(() => console.log("behavior checks completed"))
  .catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script,
             str(ROOT / "radioinspector/web/static/archive-capacity.js")],
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("behavior checks completed", result.stdout, "Node exited before async checks completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
