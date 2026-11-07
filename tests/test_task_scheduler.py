"""Runtime-only планировщик ControlTask и восстановление состояния."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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

    def test_failed_save_is_retried_until_terminal_state_persists(self) -> None:
        scheduler = self.scheduler()
        scheduler.enqueue(self.task())
        scheduler.configure(True)
        scheduler.run_once(BASE)
        scheduler.run_once(BASE + timedelta(seconds=10)
        )
        # Переводим в running повторно, чтобы проверить именно completion-time failure.
        stored = self.book_task()
        self.assertEqual(stored["status"], STATUS_COMPLETED)

        scheduler.enqueue(self.task("retry", start_at=BASE, duration_s=10))
        original_save = scheduler.book.save
        calls = 0

        def flaky_save():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary write failure")
            return original_save()

        with patch.object(scheduler.book, "save", side_effect=flaky_save) as save:
            with self.assertRaises(OSError):
                scheduler.run_once(BASE + timedelta(seconds=20))
            scheduler.run_once(BASE + timedelta(seconds=30))
            self.assertEqual(scheduler.book.get("retry").status, STATUS_COMPLETED)
            self.assertEqual(save.call_count, 2)
        scheduler.close()
        self.assertEqual(self.book_task("retry")["status"], STATUS_COMPLETED)
        self.assertEqual(save.call_count, 2)
        self.assertIsNotNone(original_save)

    def test_second_owner_cannot_recover_or_overwrite_book(self) -> None:
        first = self.scheduler()
        first.enqueue(self.task())
        before = self.path.read_text()
        with self.assertRaisesRegex(RuntimeError, "другим scheduler"):
            TaskScheduler(TaskBook(self.path).load())
        self.assertEqual(self.path.read_text(), before)
        first.configure(True)
        first.run_once(BASE)
        first.run_once(BASE + timedelta(seconds=10))
        first.close()
        self.assertEqual(self.book_task()["status"], STATUS_COMPLETED)
        second = self.scheduler()
        self.assertFalse(second.status()["enabled"])

    def test_closed_scheduler_cannot_overwrite_new_owner(self) -> None:
        first = self.scheduler()
        first.enqueue(self.task())
        first.close()
        second = self.scheduler()
        second.enqueue(self.task("new-owner"))
        with self.assertRaisesRegex(RuntimeError, "закрыт"):
            first.enqueue(self.task("stale-owner"))
        tasks = json.loads(self.path.read_text())["tasks"]
        self.assertEqual({item["name"] for item in tasks}, {"monitor", "new-owner"})

    def test_empty_optional_input_path_is_absent(self) -> None:
        scheduler = self.scheduler()
        scheduler.enqueue(
            self.task(task_file="", panorama_file=str(self.input_file))
        )
        scheduler.configure(True)
        status = scheduler.run_once(BASE)
        self.assertEqual(status["tasks"]["running"], 1)
        self.assertEqual(self.book_task()["status"], STATUS_RUNNING)
        self.assertFalse(self.book_task()["outcome"])

    def test_concurrent_enable_cannot_reopen_closed_scheduler(self) -> None:
        first = self.scheduler()
        first.enqueue(self.task())
        entered = threading.Event()
        proceed = threading.Event()
        real_lock = first._lock

        class GateLock:
            def __init__(self) -> None:
                self.first = True

            def __enter__(self):
                if self.first:
                    self.first = False
                    entered.set()
                    if not proceed.wait(2):
                        raise TimeoutError("configure gate timeout")
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        first._lock = GateLock()
        errors = []

        def enable() -> None:
            try:
                first.configure(True)
            except Exception as exc:  # noqa: BLE001 - проверка типа ошибки ниже
                errors.append(exc)

        thread = threading.Thread(target=enable)
        thread.start()
        self.assertTrue(entered.wait(2))
        first.close()
        second = self.scheduler()
        second.enqueue(self.task("new-owner"))
        proceed.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertFalse(first.status()["enabled"])
        self.assertIsNone(first.run_once(BASE))
        tasks = json.loads(self.path.read_text())["tasks"]
        self.assertEqual({item["name"] for item in tasks}, {"monitor", "new-owner"})

    def test_reenable_waits_for_previous_thread_to_finish(self) -> None:
        scheduler = self.scheduler()
        wait_returned = threading.Event()
        allow_exit = threading.Event()

        class GateEvent:
            def __init__(self) -> None:
                self.first = True
                self.actual = threading.Event()

            def wait(self, timeout):
                if self.first:
                    self.first = False
                    wait_returned.set()
                    if not allow_exit.wait(2):
                        raise TimeoutError("background gate timeout")
                    return True
                return self.actual.wait(timeout)

            def set(self) -> None:
                self.actual.set()

            def clear(self) -> None:
                self.actual.clear()

            def is_set(self) -> bool:
                return self.actual.is_set()

        scheduler._stop = GateEvent()
        scheduler._enabled = True
        scheduler._thread = threading.Thread(target=scheduler._background, daemon=True)
        scheduler._thread.start()
        old_thread = scheduler._thread
        self.assertTrue(wait_returned.wait(2))
        disable = threading.Thread(target=lambda: scheduler.configure(False))
        enable = threading.Thread(target=lambda: scheduler.configure(True))
        disable.start()
        enable.start()
        allow_exit.set()
        disable.join(3)
        enable.join(3)
        self.assertFalse(disable.is_alive())
        self.assertFalse(enable.is_alive())
        status = scheduler.status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["scheduler_alive"])
        self.assertIsNot(scheduler._thread, old_thread)

    def test_repeated_enable_does_not_join_healthy_thread(self) -> None:
        scheduler = self.scheduler(interval_seconds=0.1)
        scheduler.configure(True)
        with patch.object(
            scheduler, "_join", side_effect=AssertionError("healthy thread joined")
        ):
            status = scheduler.configure(True)
        self.assertTrue(status["enabled"])
        self.assertTrue(status["scheduler_alive"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_atomic_save_does_not_follow_predictable_tmp_symlink(self) -> None:
        target = Path(self.temporary.name) / "victim.txt"
        target.write_text("do not truncate", encoding="utf-8")
        predictable = self.path.with_name(self.path.name + ".tmp")
        predictable.symlink_to(target)
        TaskBook(self.path).save()
        self.assertEqual(target.read_text(encoding="utf-8"), "do not truncate")
        self.assertTrue(predictable.is_symlink())
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(
            json.loads(self.path.read_text())["format"], TaskBook.FORMAT
        )

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_atomic_save_preserves_permissions_and_defaults_to_owner_only(self) -> None:
        book = TaskBook(self.path)
        previous_umask = os.umask(0o022)
        try:
            book.save()
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            os.chmod(self.path, 0o600)
            book.save()
        finally:
            os.umask(previous_umask)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

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
