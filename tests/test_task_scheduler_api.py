"""HTTP-интеграция фонового планировщика ControlTask."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radioinspector.session_service import SessionService
from radioinspector.task_scheduler import TaskScheduler
from radioinspector.tasks import ControlTask, TaskBook
from radioinspector.web.server import create_server


BASE = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


class TaskSchedulerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.book_path = Path(self.temporary.name) / "tasks.json"
        self.input_path = Path(self.temporary.name) / "scan.json"
        self.input_path.write_text("{}\n", encoding="utf-8")
        self.scheduler = TaskScheduler(
            TaskBook(self.book_path),
            clock=lambda: BASE,
            interval_seconds=60,
        )
        self.sessions = SessionService()
        self.server = create_server(
            host="127.0.0.1", port=0, sessions=self.sessions, task_scheduler=self.scheduler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.sessions.close()
        self.temporary.cleanup()

    def request(self, method: str, path: str, payload=None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        body = None if payload is None else json.dumps(payload)
        connection.request(
            method,
            path,
            body=body,
            headers={} if body is None else {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)

    def test_status_enable_complete_disable_and_capabilities(self) -> None:
        self.scheduler.enqueue(
            ControlTask(
                name="api-monitor",
                task_file=str(self.input_path),
                start_at=BASE,
                duration_s=10,
            )
        )
        status, response = self.request("GET", "/api/v1/task-scheduler")
        self.assertEqual(status, 200)
        self.assertFalse(response["data"]["enabled"])
        self.assertFalse(response["data"]["policy_persistent"])
        self.assertTrue(response["data"]["restart_requires_opt_in"])
        self.assertEqual(response["data"]["tasks"]["total"], 1)

        status, response = self.request("POST", "/api/v1/task-scheduler", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(response["data"]["enabled"])
        self.assertTrue(response["data"]["scheduler_alive"])
        self.scheduler.run_once(BASE)
        self.scheduler.run_once(BASE + timedelta(seconds=10))
        status, response = self.request("GET", "/api/v1/task-scheduler")
        self.assertEqual(response["data"]["runtime"]["completed_tasks"], 1)
        self.assertEqual(response["data"]["tasks"]["completed"], 1)

        status, capabilities = self.request("GET", "/api/v1/capabilities")
        self.assertEqual(status, 200)
        features = capabilities["data"]["features"]
        self.assertTrue(features["control_task_scheduler"])
        self.assertTrue(features["control_task_scheduler_opt_in_required"])
        self.assertFalse(features["control_task_scheduler_policy_persistent"])

        status, response = self.request("POST", "/api/v1/task-scheduler", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(response["data"]["enabled"])
        self.assertFalse(response["data"]["scheduler_alive"])
        self.server.control_scheduler.close()
        second = TaskScheduler(TaskBook(self.book_path))
        second.close()

    def test_rejects_non_boolean_and_extra_fields(self) -> None:
        status, response = self.request("POST", "/api/v1/task-scheduler", {"enabled": 1})
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], "invalid_request")
        status, response = self.request(
            "POST", "/api/v1/task-scheduler", {"enabled": True, "interval": 0.1}
        )
        self.assertEqual(status, 400)
        self.assertIn("enabled", response["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
