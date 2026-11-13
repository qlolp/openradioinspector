"""Создание ControlTask через версионированный HTTP API."""

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
from radioinspector.tasks import TaskBook
from radioinspector.web.server import create_server


BASE = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


class TaskSchedulerCreateApiTests(unittest.TestCase):
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
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.scheduler.close()
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

    def valid_payload(self) -> dict:
        return {
            "name": "api-created",
            "task_file": str(self.input_path),
            "start_at": "2026-11-07T14:00:00+02:00",
            "duration_s": 15,
            "operator": "Оператор",
            "equipment": "tinySA Ultra",
        }

    def test_create_is_atomic_sanitized_and_event_free(self) -> None:
        before_events = self.sessions.health()["last_event_id"]
        status, response = self.request("POST", "/api/v1/task-scheduler/tasks", self.valid_payload())
        self.assertEqual(status, 201)
        created = response["data"]
        self.assertEqual(created["name"], "api-created")
        self.assertEqual(created["start_at"], "2026-11-07T12:00:00Z")
        self.assertNotIn("task_file", created)
        self.assertNotIn("panorama_file", created)
        self.assertEqual(self.sessions.health()["last_event_id"], before_events)

        status, response = self.request("GET", "/api/v1/task-scheduler/tasks")
        self.assertEqual(response["data"]["count"], 1)
        self.assertEqual(response["data"]["items"][0]["equipment"], "tinySA Ultra")
        status, capabilities = self.request("GET", "/api/v1/capabilities")
        self.assertTrue(capabilities["data"]["features"]["control_task_scheduler_task_create"])

        before = self.book_path.read_bytes()
        status, duplicate = self.request("POST", "/api/v1/task-scheduler/tasks", self.valid_payload())
        self.assertEqual(status, 400)
        self.assertEqual(duplicate["error"]["code"], "invalid_request")
        self.assertEqual(self.book_path.read_bytes(), before)

        invalid = self.valid_payload()
        invalid["outcome"] = "FORGED"
        status, rejected = self.request("POST", "/api/v1/task-scheduler/tasks", invalid)
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"]["code"], "invalid_request")
        self.assertEqual(self.book_path.read_bytes(), before)

    def test_invalid_task_does_not_create_book_or_events(self) -> None:
        before_events = self.sessions.health()["last_event_id"]
        for changes in (
            {"duration_s": 0},
            {"duration_s": True},
            {"task_file": None, "panorama_file": None},
            {"start_at": "not-a-date"},
        ):
            with self.subTest(changes=changes):
                payload = self.valid_payload()
                payload.update(changes)
                status, response = self.request("POST", "/api/v1/task-scheduler/tasks", payload)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"]["code"], "invalid_request")
                self.assertFalse(self.book_path.exists())
                self.assertEqual(self.sessions.health()["last_event_id"], before_events)


if __name__ == "__main__":
    unittest.main(verbosity=2)
