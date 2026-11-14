"""Windows: отмена queued ControlTask через API."""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from radioinspector.session_service import SessionService
from radioinspector.task_scheduler import TaskScheduler
from radioinspector.tasks import ControlTask, TaskBook
from radioinspector.web.server import create_server
from radioinspector.windows.client import PlatformClient
from radioinspector.windows.scheduler_app import SchedulerWindowsApp


BASE = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


class WindowsTaskCancelTests(unittest.TestCase):
    def test_real_http_cancel_and_ui_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scheduler = TaskScheduler(TaskBook(Path(temporary) / "tasks.json"), interval_seconds=60)
            sessions = SessionService()
            server = create_server(
                host="127.0.0.1", port=0, sessions=sessions, task_scheduler=scheduler
            )
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                input_path = Path(temporary) / "scan.json"
                input_path.write_text("{}\n", encoding="utf-8")
                scheduler.enqueue(
                    task=ControlTask(
                        name="windows/cancel",
                        task_file=str(input_path),
                        start_at=BASE,
                        duration_s=10,
                    )
                )
                host, port = server.server_address
                client = PlatformClient(f"http://{host}:{port}")
                cancelled = client.cancel_task("windows/cancel")
                self.assertEqual(cancelled["name"], "windows/cancel")
                self.assertEqual(cancelled["status"], "Снято с выполнения")
                self.assertEqual(cancelled["outcome"], "принудительно завершено оператором")
                self.assertNotIn("task_file", cancelled)

                class Value:
                    def __init__(self):
                        self.value = ""

                    def set(self, value):
                        self.value = value

                class Client:
                    def __init__(self):
                        self.calls = []

                    def cancel_task(self, name):
                        self.calls.append(name)
                        return {
                            "name": name,
                            "status": "Снято с выполнения",
                            "outcome": "принудительно завершено оператором",
                        }

                app = SchedulerWindowsApp.__new__(SchedulerWindowsApp)
                app.client = Client()
                app.task_cancel_var = Value()
                app._destroy_form = lambda _window: None
                app._submit_action = lambda _description, operation, callback: callback(operation())
                app._cancel_task(object(), "windows/cancel")
                self.assertEqual(app.client.calls, ["windows/cancel"])
                self.assertIn("windows/cancel", app.task_cancel_var.value)
                self.assertIn("Снято с выполнения", app.task_cancel_var.value)
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
