"""Windows: создание ControlTask с нормализацией полей и реальным POST."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from radioinspector.session_service import SessionService
from radioinspector.task_scheduler import TaskScheduler
from radioinspector.tasks import TaskBook
from radioinspector.web.server import create_server
from radioinspector.windows.client import PlatformClient, PlatformClientError
from radioinspector.windows.scheduler_app import SchedulerWindowsApp, task_create_payload


class WindowsTaskCreateTests(unittest.TestCase):
    def test_payload_normalizes_utc_and_rejects_invalid_values(self) -> None:
        payload = task_create_payload({
            "name": " windows-task ",
            "task_file": " /tmp/scan.json ",
            "start_at": "2026-11-07T14:00:00+02:00",
            "duration_s": 15,
            "equipment": " tinySA Ultra ",
        })
        self.assertEqual(payload["name"], "windows-task")
        self.assertEqual(payload["task_file"], "/tmp/scan.json")
        self.assertEqual(payload["start_at"], "2026-11-07T12:00:00Z")
        self.assertEqual(payload["duration_s"], 15.0)
        self.assertEqual(payload["equipment"], "tinySA Ultra")
        for changes in (
            {"duration_s": 0},
            {"duration_s": True},
            *({"duration_s": value} for value in (
                "", " ", "abc", "true", "0", "-1", "nan", "inf", "1e309",
                float("nan"), float("inf"), -float("inf"), [], {}, None, 10**400,
            )),
            {"start_at": "not-a-date"},
            {"name": " "},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    task_create_payload({**payload, **changes})

        for value, expected in (("60", 60.0), (" 0.25 ", 0.25), ("1e2", 100.0)):
            with self.subTest(value=value):
                normalized = task_create_payload({**payload, "duration_s": value})
                self.assertEqual(normalized["duration_s"], expected)
                self.assertIs(type(normalized["duration_s"]), float)

    def test_real_http_create_and_ui_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scheduler = TaskScheduler(TaskBook(Path(temporary) / "tasks.json"), interval_seconds=60)
            sessions = SessionService()
            server = create_server(
                host="127.0.0.1", port=0, sessions=sessions, task_scheduler=scheduler
            )
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                client = PlatformClient(f"http://{host}:{port}")
                payload = task_create_payload({
                    "name": "windows-http",
                    "task_file": str(Path(temporary) / "scan.json"),
                    "start_at": "2026-11-07T12:00:00Z",
                    "duration_s": 10,
                })
                created = client.create_task(payload)
                self.assertEqual(created["name"], "windows-http")
                self.assertNotIn("task_file", created)
                self.assertEqual(client.task_scheduler_tasks()[0]["name"], "windows-http")
                # Text conversion belongs to the form; the API remains strict.
                with self.assertRaises(PlatformClientError):
                    client.create_task({**payload, "duration_s": "60"})

                app = SchedulerWindowsApp.__new__(SchedulerWindowsApp)
                app.client = client
                app._action_running = app._closed = False
                app._revision = 0
                window = app._active_form = object()
                app._task_create_context = (window, client, app._revision)
                app.task_create_var = type("Value", (), {"value": "", "set": lambda self, value: setattr(self, "value", value)})()
                app._destroy_form = lambda _window: None
                app._submit_action = lambda _description, operation, callback: callback(operation())
                app._create_task(window, {
                    "name": type("Value", (), {"get": lambda self: "windows-ui"})(),
                    "task_file": type("Value", (), {"get": lambda self: "/tmp/ui.json"})(),
                    "start_at": type("Value", (), {"get": lambda self: "2026-11-07T12:00:00Z"})(),
                    "duration_s": type("Value", (), {"get": lambda self: "20.0"})(),
                    "equipment": type("Value", (), {"get": lambda self: "AR-DV1"})(),
                })
                self.assertIn("windows-ui", app.task_create_var.value)
                self.assertIn("В очереди на выполнение", app.task_create_var.value)
            finally:
                server.automatic_detection.close()
                server.automatic_handoff.close()
                server.automatic_completion.close()
                server.control_scheduler.close()
                server.worker_manager.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                sessions.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
