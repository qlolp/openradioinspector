"""Runtime-only фоновый планировщик ``ControlTask`` без скрытых измерений."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


def _acquire_book_lock(path: Path) -> Any:
    """Удерживает OS-level lock до close, не полагаясь на наличие lock-файла."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (OSError, IOError) as exc:
        handle.close()
        raise RuntimeError(f"TaskBook уже используется другим scheduler: {path}") from exc


def _release_book_lock(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()

from .tasks import (
    OUTCOME_APPLICATION_CRASH,
    OUTCOME_FILE_NOT_FOUND,
    OUTCOME_STOPPED_BY_OPERATOR,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    ControlTask,
    TaskBook,
)


class TaskScheduler:
    """Запускает due-задачи и не создаёт измерительных данных сам."""

    def __init__(
        self,
        book: TaskBook,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        on_start: Optional[Callable[[ControlTask], None]] = None,
        on_complete: Optional[Callable[[ControlTask], None]] = None,
        interval_seconds: float = 1.0,
    ) -> None:
        if not 0.1 <= float(interval_seconds) <= 60.0:
            raise ValueError("interval_seconds должен быть в диапазоне 0,1…60")
        self.book = book
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._on_start = on_start or (lambda _task: None)
        self._on_complete = on_complete or (lambda _task: None)
        self._interval = float(interval_seconds)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._enabled = False
        self._closed = False
        self._dirty = False
        self._runtime: dict[str, Any] = {
            "last_result": "idle",
            "last_error": None,
            "last_task_name": None,
            "started_tasks": 0,
            "completed_tasks": 0,
            "aborted_tasks": 0,
        }
        self._book_lock = _acquire_book_lock(self.book.path)
        try:
            self.book.load()
            self._recover_interrupted()
        except Exception:
            _release_book_lock(self._book_lock)
            raise

    def _save_locked(self) -> None:
        if self._dirty:
            self.book.save()
            self._dirty = False

    def _recover_interrupted(self) -> None:
        changed = False
        for task in self.book.tasks:
            if task.status == STATUS_RUNNING:
                task.abort(OUTCOME_APPLICATION_CRASH)
                self._runtime["aborted_tasks"] += 1
                changed = True
        if changed:
            self._dirty = True
            self._save_locked()
            self._runtime["last_result"] = "interrupted_recovered"

    def enqueue(self, task: ControlTask) -> ControlTask:
        """Добавляет задачу оператора без автоматического включения scheduler."""
        with self._lock:
            if self.book.get(task.name) is not None:
                raise ValueError(f"Задание уже существует: {task.name}")
            task.enqueue()
            self.book.add(task)
            self._dirty = True
            self._save_locked()
            return task

    def configure(self, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise TypeError("enabled должен быть JSON boolean")
        if self._closed:
            raise RuntimeError("Scheduler уже закрыт")
        with self._lock:
            self._enabled = enabled
            self._runtime["last_result"] = "enabled" if enabled else "disabled"
            if enabled:
                self._stop.clear()
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(
                        target=self._background,
                        name="control-task-scheduler",
                        daemon=True,
                    )
                    self._thread.start()
            else:
                self._stop.set()
        if not enabled:
            self._join()
        return self.status()

    def _join(self) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self._interval * 4 + 0.5)
            )

    def _background(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - изоляция фонового цикла
                with self._lock:
                    self._runtime["last_result"] = "scheduler_error"
                    self._runtime["last_error"] = str(exc)

    def run_once(self, moment: Optional[datetime] = None) -> Optional[dict[str, Any]]:
        """Выполняет один проход без ожидания и возвращает фактический status."""
        with self._lock:
            if not self._enabled:
                return None
            now = moment or self._clock()
            if now.tzinfo is None:
                raise ValueError("Время прохода должно быть timezone-aware")
            changed = False
            self._runtime["last_error"] = None
            for task in self.book.tasks:
                try:
                    if task.status == STATUS_RUNNING:
                        ends_at = task.ends_at()
                        if ends_at is not None and now >= ends_at:
                            self._on_complete(task)
                            task.complete()
                            task.completed_cycles += 1
                            self._runtime["completed_tasks"] += 1
                            self._runtime["last_task_name"] = task.name
                            self._runtime["last_result"] = "completed"
                            changed = True
                    elif task.is_due(now):
                        self._require_input_files(task)
                        task.start()
                        self._on_start(task)
                        self._runtime["started_tasks"] += 1
                        self._runtime["last_task_name"] = task.name
                        self._runtime["last_result"] = "started"
                        changed = True
                except Exception as exc:  # noqa: BLE001 - независимость задач
                    task.abort(str(exc))
                    self._runtime["aborted_tasks"] += 1
                    self._runtime["last_task_name"] = task.name
                    self._runtime["last_result"] = "task_error"
                    self._runtime["last_error"] = str(exc)
                    changed = True
            if changed:
                self._dirty = True
            self._save_locked()
            return self.status()

    @staticmethod
    def _require_input_files(task: ControlTask) -> None:
        for value in (task.task_file, task.panorama_file):
            if value is not None and not Path(value).is_file():
                raise FileNotFoundError(OUTCOME_FILE_NOT_FOUND)

    def status(self) -> dict[str, Any]:
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            return {
                "enabled": self._enabled,
                "scheduler_alive": alive,
                "policy_persistent": False,
                "restart_requires_opt_in": True,
                "runtime": dict(self._runtime),
                "tasks": {
                    "total": len(self.book.tasks),
                    "running": sum(
                        task.status == STATUS_RUNNING for task in self.book.tasks
                    ),
                    "completed": sum(
                        task.status == STATUS_COMPLETED for task in self.book.tasks
                    ),
                },
            }

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        """Останавливает thread и явно фиксирует operator abort активных задач."""
        if self._closed:
            return
        self.configure(False)
        self._closed = True
        changed = False
        try:
            with self._lock:
                for task in self.book.tasks:
                    if task.status == STATUS_RUNNING:
                        task.abort(OUTCOME_STOPPED_BY_OPERATOR)
                        self._runtime["aborted_tasks"] += 1
                        changed = True
                if changed:
                    self._dirty = True
                self._save_locked()
                self._runtime["last_result"] = "closed"
        finally:
            _release_book_lock(self._book_lock)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radioinspector-task-scheduler",
        description="Фоновый планировщик ControlTask с явным opt-in",
    )
    parser.add_argument("--book", required=True, help="JSON TaskBook")
    parser.add_argument(
        "--enable", action="store_true", help="явно разрешить запуск заданий"
    )
    parser.add_argument(
        "--once", action="store_true", help="один проход без ожидания"
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument(
        "--timeout", type=float, default=0.0, help="ограничение continuous run, секунды"
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.enable:
        print("Для запуска требуется явный --enable", file=sys.stderr)
        return 2
    scheduler = TaskScheduler(TaskBook(args.book), interval_seconds=args.interval)
    try:
        scheduler.configure(True)
        if args.once:
            result = scheduler.run_once()
        elif args.timeout > 0:
            import time

            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))
            result = scheduler.status()
        else:
            result = scheduler.status()
            print(json.dumps(result, ensure_ascii=False))
            while True:
                import time

                time.sleep(args.interval)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        scheduler.close()


if __name__ == "__main__":
    raise SystemExit(main())
