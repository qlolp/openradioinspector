"""Отмена ожидающих ControlTask через HTTP API."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from radioinspector.session_service import SessionService
from radioinspector.task_scheduler import TaskScheduler
from radioinspector.tasks import ControlTask, TaskBook
from radioinspector.web.server import create_server


BASE = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


class TaskSchedulerCancelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.book_path = Path(self.temporary.name) / "tasks.json"
        self.input_path = Path(self.temporary.name) / "scan.json"
        self.input_path.write_text("{}\n", encoding="utf-8")
        self.scheduler = TaskScheduler(TaskBook(self.book_path), interval_seconds=60)
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

    def test_cancel_only_queue_is_atomic_and_sanitized(self) -> None:
        name = "cancel/me"
        task = ControlTask(
            name=name,
            task_file=str(self.input_path),
            start_at=BASE,
            duration_s=10,
            equipment="tinySA Ultra",
        )
        self.scheduler.enqueue(task)
        before_events = self.sessions.health()["last_event_id"]
        status, response = self.request(
            "POST", "/api/v1/task-scheduler/tasks/cancel%2Fme/cancel"
        )
        self.assertEqual(status, 200)
        cancelled = response["data"]
        self.assertEqual(cancelled["name"], name)
        self.assertEqual(cancelled["status"], "Снято с выполнения")
        self.assertEqual(cancelled["outcome"], "принудительно завершено оператором")
        self.assertNotIn("task_file", cancelled)
        self.assertEqual(self.sessions.health()["last_event_id"], before_events)

        before = self.book_path.read_bytes()
        status, second = self.request(
            "POST", "/api/v1/task-scheduler/tasks/cancel%2Fme/cancel"
        )
        self.assertEqual(status, 400)
        self.assertEqual(second["error"]["code"], "invalid_request")
        self.assertEqual(self.book_path.read_bytes(), before)
        status, missing = self.request("POST", "/api/v1/task-scheduler/tasks/missing/cancel")
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"]["code"], "task_not_found")
        self.assertEqual(self.book_path.read_bytes(), before)

        status, capabilities = self.request("GET", "/api/v1/capabilities")
        self.assertTrue(capabilities["data"]["features"]["control_task_scheduler_task_cancel"])

    def test_running_task_is_not_cancelled(self) -> None:
        task = ControlTask(
            name="running-task",
            task_file=str(self.input_path),
            start_at=BASE,
            duration_s=10,
        )
        self.scheduler.enqueue(task)
        task.start()
        before = self.book_path.read_bytes()
        status, response = self.request(
            "POST", "/api/v1/task-scheduler/tasks/running-task/cancel"
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(self.book_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
