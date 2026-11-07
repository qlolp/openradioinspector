"""Windows-клиент глобального TaskScheduler и его явный opt-in."""

from __future__ import annotations

import threading
import tomllib
import unittest
from pathlib import Path

from radioinspector.windows.client import PlatformClient, PlatformClientError
from radioinspector.windows.scheduler_app import (
    SchedulerWindowsApp,
    task_scheduler_tasks_text,
    task_scheduler_text,
)
from radioinspector.web.server import create_server


class WindowsSchedulerTests(unittest.TestCase):
    def test_status_text_uses_actual_runtime_and_isolates_error(self) -> None:
        status = {
            "enabled": True,
            "scheduler_alive": True,
            "runtime": {"last_result": "completed"},
            "tasks": {"total": 3, "running": 1, "completed": 2},
        }
        text = task_scheduler_text(status)
        self.assertIn("включён", text)
        self.assertIn("thread active", text)
        self.assertIn("всего 3", text)
        self.assertIn("в работе 1", text)
        self.assertIn("готово 2", text)
        self.assertIn("остальные функции", task_scheduler_text(None, "old server"))

    def test_task_list_text_preserves_taskbook_fields_and_isolates_error(self) -> None:
        text = task_scheduler_tasks_text([
            {
                "name": "windows-monitor",
                "status": "completed",
                "start_at": "2026-11-07T12:00:00Z",
                "outcome": "OUTCOME_SUCCESS",
                "equipment": "tinySA Ultra + AR-DV1",
            },
            {"name": "windows-queued", "status": "queued"},
        ])
        self.assertIn("windows-monitor · completed · 2026-11-07T12:00:00Z", text)
        self.assertIn("OUTCOME_SUCCESS", text)
        self.assertIn("tinySA Ultra + AR-DV1", text)
        self.assertIn("windows-queued · queued · время не задано", text)
        self.assertIn("Оборудование не указано", text)
        self.assertIn("нет задач", task_scheduler_tasks_text([]))
        self.assertIn(
            "статус scheduler продолжает обновляться",
            task_scheduler_tasks_text([], "old server"),
        )

    def test_real_http_status_enable_disable_and_boolean_validation(self) -> None:
        server = create_server(host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            client = PlatformClient(f"http://{host}:{port}")
            initial = client.task_scheduler()
            self.assertFalse(initial["enabled"])
            self.assertFalse(initial["policy_persistent"])
            self.assertTrue(initial["restart_requires_opt_in"])
            self.assertEqual(client.task_scheduler_tasks(), [])
            enabled = client.configure_task_scheduler(True)
            self.assertTrue(enabled["enabled"])
            self.assertTrue(enabled["scheduler_alive"])
            disabled = client.configure_task_scheduler(False)
            self.assertFalse(disabled["enabled"])
            with self.assertRaisesRegex(TypeError, "JSON boolean"):
                client.configure_task_scheduler(1)
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

    def test_snapshot_isolates_scheduler_error_and_action_captures_boolean(self) -> None:
        server = create_server(host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            client = PlatformClient(f"http://{host}:{port}")

            def unavailable():
                raise PlatformClientError("unsupported route")

            client.task_scheduler = unavailable
            snapshot = client.snapshot()
            self.assertIsNone(snapshot.task_scheduler)
            self.assertEqual(snapshot.task_scheduler_error, "unsupported route")
            self.assertEqual(snapshot.task_scheduler_tasks, [])
            self.assertIsNone(snapshot.task_scheduler_tasks_error)
            self.assertEqual(snapshot.sessions, [])

            client.task_scheduler = lambda: {"enabled": False}
            client.task_scheduler_tasks = unavailable
            snapshot = client.snapshot()
            self.assertEqual(snapshot.task_scheduler, {"enabled": False})
            self.assertIsNone(snapshot.task_scheduler_error)
            self.assertEqual(snapshot.task_scheduler_tasks_error, "unsupported route")
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

        class RecordingClient:
            def __init__(self):
                self.calls = []

            def configure_task_scheduler(self, enabled):
                self.calls.append(enabled)
                return {"enabled": enabled}

        app = SchedulerWindowsApp.__new__(SchedulerWindowsApp)
        app.client = RecordingClient()
        destroyed = []
        operations = []
        app._destroy_form = lambda window: destroyed.append(window)
        app._submit_action = lambda _description, operation: operations.append(operation)
        window = object()
        app._configure_task_scheduler(True, window)
        self.assertEqual(destroyed, [window])
        operation = operations.pop(0)
        operation()
        self.assertEqual(app.client.calls, [True])
        with self.assertRaisesRegex(TypeError, "JSON boolean"):
            app._configure_task_scheduler(1, object())

    def test_task_list_rejects_inconsistent_contract(self) -> None:
        client = PlatformClient("http://127.0.0.1:8770")
        client._request = lambda *_args: {"items": [{}], "count": 2}
        with self.assertRaises(PlatformClientError) as caught:
            client.task_scheduler_tasks()
        self.assertEqual(caught.exception.code, "invalid_task_list")

    def test_installed_launcher_uses_scheduler_app(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)
        self.assertEqual(
            project["project"]["scripts"]["radioinspector-windows"],
            "radioinspector.windows.scheduler_app:main",
        )
        self.assertIn(
            "from .scheduler_app import main",
            (root / "radioinspector" / "windows" / "__main__.py").read_text(
                encoding="utf-8"
            ),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
