"""Планировщик плановых заданий радиоконтроля.

Исходный комплекс выполняет задание автоматически, если в назначенный
момент запущена программа управления средствами измерений. Здесь ту же
роль выполняет периодический вызов :func:`advance_tasks`: наступившие
задания переводятся в выполнение, истёкшие — завершаются, отсутствующий
файл задания фиксируется как ошибка.

Одновременно выполняется одно задание. Если к моменту проверки наступило
несколько окон, запускается задание с наиболее поздним временем старта,
а более ранние снимаются с пометкой «принудительно завершено другой задачей».
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .tasks import (
    OUTCOME_APPLICATION_CRASH,
    OUTCOME_FILE_NOT_FOUND,
    OUTCOME_STOPPED_BY_TASK,
    OUTCOME_WINDOW_MISSED,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    ControlTask,
    TaskBook,
    as_utc,
)

#: Вызывается после перевода задания в выполнение. Исключение фиксируется
#: как аварийное завершение приложения.
TaskRunner = Callable[[ControlTask], None]


@dataclass(frozen=True)
class TaskTransition:
    """Один переход задания, сделанный планировщиком."""

    name: str
    status: str
    action: str
    outcome: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "action": self.action,
            "outcome": self.outcome,
            "note": self.note,
        }


def _window_closed(task: ControlTask, moment: datetime) -> bool:
    """Истекло ли плановое окно задания.

    Нулевая длительность означает, что момент окончания не задан: задание
    остаётся в выполнении, пока его не снимут.
    """
    if task.duration_s <= 0 or task.start_at is None:
        return False
    end = task.ends_at()
    if end is None:
        return False
    return as_utc(moment) >= as_utc(end)


def _file_exists(raw: str, base: Path) -> bool:
    path = Path(raw)
    options = [path] if path.is_absolute() else [base / path, path]
    return any(option.is_file() for option in options)


def _sources_exist(task: ControlTask, base: Path) -> bool:
    """Все указанные файлы задания существуют."""
    specified = [raw for raw in (task.task_file, task.panorama_file) if raw]
    if not specified:
        return False
    return all(_file_exists(raw, base) for raw in specified)


def advance_tasks(
    book: TaskBook,
    moment: Optional[datetime] = None,
    runner: Optional[TaskRunner] = None,
) -> list[TaskTransition]:
    """Продвигает задания по времени и возвращает сделанные переходы.

    База не записывается на диск: это делает вызывающий код, если список
    переходов непуст.
    """
    moment = as_utc(moment or datetime.now(timezone.utc))
    base = book.path.parent
    transitions: list[TaskTransition] = []

    for task in list(book.tasks):
        if task.status == STATUS_RUNNING and _window_closed(task, moment):
            task.complete()
            transitions.append(
                TaskTransition(task.name, STATUS_COMPLETED, "выполнено")
            )

    due = [task for task in book.tasks if task.is_due(moment)]
    due.sort(key=lambda task: as_utc(task.start_at) if task.start_at else moment)

    ready: list[ControlTask] = []
    for task in due:
        if not _sources_exist(task, base):
            task.abort(OUTCOME_FILE_NOT_FOUND)
            transitions.append(
                TaskTransition(task.name, STATUS_ABORTED, "снято", task.outcome)
            )
            continue
        if _window_closed(task, moment):
            task.abort(OUTCOME_WINDOW_MISSED)
            transitions.append(
                TaskTransition(task.name, STATUS_ABORTED, "снято", task.outcome)
            )
            continue
        ready.append(task)

    if not ready:
        return transitions

    chosen = ready[-1]
    for task in ready[:-1]:
        task.abort(OUTCOME_STOPPED_BY_TASK)
        transitions.append(
            TaskTransition(task.name, STATUS_ABORTED, "снято", task.outcome)
        )
    for other in book.tasks:
        if other is not chosen and other.status == STATUS_RUNNING:
            other.abort(OUTCOME_STOPPED_BY_TASK)
            transitions.append(
                TaskTransition(other.name, STATUS_ABORTED, "снято", other.outcome)
            )

    chosen.start()
    transitions.append(TaskTransition(chosen.name, STATUS_RUNNING, "запущено"))
    if runner is None:
        return transitions
    try:
        runner(chosen)
    except Exception as exc:  # noqa: BLE001 - граница внешнего исполнителя
        chosen.abort(OUTCOME_APPLICATION_CRASH)
        transitions.append(
            TaskTransition(
                chosen.name,
                STATUS_ABORTED,
                "снято",
                chosen.outcome,
                note=str(exc),
            )
        )
        return transitions
    chosen.completed_cycles += 1
    return transitions


class TaskScheduler:
    """Периодический запуск :func:`advance_tasks` для одной базы заданий."""

    def __init__(
        self,
        book: TaskBook,
        runner: Optional[TaskRunner] = None,
        interval_s: float = 1.0,
    ) -> None:
        self.book = book
        self.runner = runner
        self.interval_s = interval_s if interval_s > 0 else 1.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def tick(self, moment: Optional[datetime] = None) -> list[TaskTransition]:
        """Один проход планировщика. Изменения сразу записываются в базу."""
        changes = advance_tasks(self.book, moment, self.runner)
        if changes:
            self.book.save()
        return changes

    def watch(self) -> None:
        """Цикл до :meth:`stop` или прерывания с клавиатуры."""
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        """Фоновый цикл в отдельном потоке."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.watch,
            name="radioinspector-tasks",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Останавливает фоновый цикл."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_s + 1.0))
            self._thread = None
