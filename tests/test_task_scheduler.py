"""Runtime-only планировщик ControlTask и восстановление состояния."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radioinspector.task_scheduler import TaskScheduler, main
from radioinspector.tasks import (
    OUTCOME_APPLICATION_CRASH,
    OUTCOME_FILE_NOT_FOUND,
    OUTCOME_STOPPED_BY_OPERATOR,
    STATUS_COMPLETED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    ControlTask,
    TaskBook,
)


BASE = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


class TaskSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "tasks.json"
        self.input_file = Path(self.temporary.name) / "scan.json"
        self.input_file.write_text("{}\n", encoding="utf-8")
        self.schedulers = []

    def tearDown(self) -> None:
        for scheduler in self.schedulers:
            scheduler.close()
        self.temporary.cleanup()

    def scheduler(self, **kwargs) -> TaskScheduler:
        scheduler = TaskScheduler(
            TaskBook(self.path), interval_seconds=kwargs.pop("interval_seconds", 60), **kwargs
        )
        self.schedulers.append(scheduler)
        return scheduler

    def task(self, name="monitor", **kwargs) -> ControlTask:
        values = {
            "task_file": str(self.input_file),
            "start_at": BASE,
            "duration_s": 10,
        }
        values.update(kwargs)
        return ControlTask(name=name, **values)

    def test_default_disabled_requires_opt_in(self) -> None:
        scheduler = self.scheduler()
        scheduler.enqueue(self.task())
        self.assertIsNone(scheduler.run_once(BASE + timedelta(seconds=20)))
        status = scheduler.status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["policy_persistent"])
        self.assertTrue(status["restart_requires_opt_in"])
        self.assertEqual(self.book_task()["status"], STATUS_QUEUED)

    def test_starts_only_due_and_completes_after_duration(self) -> None:
        scheduler = self.scheduler()
        scheduler.enqueue(self.task())
        scheduler.configure(True)
        early = scheduler.run_once(BASE - timedelta(seconds=1))
        self.assertEqual(early["tasks"]["running"], 0)
        started = scheduler.run_once(BASE)
        self.assertEqual(started["runtime"]["started_tasks"], 1)
        self.assertEqual(started["tasks"]["running"], 1)
        waiting = scheduler.run_once(BASE + timedelta(seconds=9))
        self.assertEqual(waiting["runtime"]["completed_tasks"], 0)
        completed = scheduler.run_once(BASE + timedelta(seconds=10))
        self.assertEqual(completed["runtime"]["completed_tasks"], 1)
        stored = self.book_task()
        self.assertEqual(stored["status"], STATUS_COMPLETED)
        self.assertEqual(stored["completed_cycles"], 1)
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_missing_file_does_not_stop_other_tasks(self) -> None:
        scheduler = self.scheduler()
        scheduler.enqueue(
            self.task("missing", task_file=str(Path(self.temporary.name) / "missing.json"))
        )
        scheduler.enqueue(self.task("valid"))
        scheduler.configure(True)
        status = scheduler.run_once(BASE)
        tasks = {item["name"]: item for item in json.loads(self.path.read_text())["tasks"]}
        self.assertEqual(tasks["missing"]["outcome"], OUTCOME_FILE_NOT_FOUND)
        self.assertEqual(tasks["valid"]["status"], STATUS_RUNNING)
        self.assertEqual(status["runtime"]["aborted_tasks"], 1)
        self.assertEqual(status["runtime"]["started_tasks"], 1)
        completed = scheduler.run_once(BASE + timedelta(seconds=10))
        self.assertEqual(completed["runtime"]["completed_tasks"], 1)

    def test_close_and_restart_recover_interrupted_tasks(self) -> None:
        scheduler = self.scheduler()
        scheduler.enqueue(self.task())
        scheduler.configure(True)
        scheduler.run_once(BASE)
        scheduler.close()
        self.assertEqual(self.book_task()["outcome"], OUTCOME_STOPPED_BY_OPERATOR)

        TaskBook(self.path).save()
        running = self.task("interrupted", status=STATUS_RUNNING)
        book = TaskBook(self.path)
        book.add(running)
        book.save()
        recovered = self.scheduler()
        self.assertEqual(self.book_task("interrupted")["outcome"], OUTCOME_APPLICATION_CRASH)
        self.assertFalse(recovered.status()["enabled"])

    def test_background_thread_runs_real_lifecycle(self) -> None:
        started = threading.Event()
        completed = threading.Event()
        scheduler = self.scheduler(
            on_start=lambda _task: started.set(),
            on_complete=lambda _task: completed.set(),
            interval_seconds=0.1,
        )
        scheduler.enqueue(
            self.task(start_at=datetime.now(timezone.utc), duration_s=0.2)
        )
        scheduler.configure(True)
        self.assertTrue(started.wait(2))
        self.assertTrue(completed.wait(2))
        scheduler.close()
        self.assertFalse(scheduler.status()["scheduler_alive"])
        self.assertEqual(self.book_task()["status"], STATUS_COMPLETED)

    def test_cli_requires_enable_and_supports_once(self) -> None:
        with redirect_stderr(io.StringIO()) as errors:
            code = main(["--book", str(self.path), "--once"])
        self.assertEqual(code, 2)
        self.assertIn("--enable", errors.getvalue())

        scheduler = self.scheduler()
        scheduler.enqueue(self.task(start_at=datetime.now(timezone.utc)))
        scheduler.close()
        with redirect_stdout(io.StringIO()) as output:
            code = main(["--book", str(self.path), "--enable", "--once"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["runtime"]["started_tasks"], 1)

    def book_task(self, name="monitor") -> dict:
        tasks = json.loads(self.path.read_text())["tasks"]
        return next(item for item in tasks if item["name"] == name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
