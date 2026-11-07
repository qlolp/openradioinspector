"""Windows-панель глобального TaskScheduler без привязки к сеансу."""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from .analytics_app import AnalyticsWindowsApp


def task_scheduler_text(
    status: Optional[dict[str, Any]], error: Optional[str] = None
) -> str:
    """Форматирует только полученные от ядра состояние и счётчики."""
    if error:
        return f"Scheduler: недоступен ({error}); остальные функции продолжаются"
    if not status:
        return "Scheduler: состояние не получено"
    runtime = status.get("runtime") or {}
    tasks = status.get("tasks") or {}
    return (
        f"Scheduler: {'включён' if status.get('enabled') else 'выключен'} · "
        f"thread {'active' if status.get('scheduler_alive') else 'stopped'} · "
        f"{runtime.get('last_result', 'idle')} · всего {tasks.get('total', 0)}, "
        f"в работе {tasks.get('running', 0)}, готово {tasks.get('completed', 0)}"
    )


def task_scheduler_tasks_text(
    tasks: Optional[list[dict[str, Any]]], error: Optional[str] = None
) -> str:
    """Форматирует read-only снимок TaskBook без обращения к её файлу."""
    if error:
        return f"Список ControlTask недоступен ({error}); статус scheduler продолжает обновляться"
    if not tasks:
        return "В TaskBook нет задач."
    lines = []
    for task in tasks:
        outcome = f" · {task['outcome']}" if task.get("outcome") else ""
        lines.append(
            f"{task.get('name') or 'Без имени'} · {task.get('status') or 'unknown'} · "
            f"{task.get('start_at') or 'время не задано'}{outcome}"
        )
        lines.append(f"  {task.get('equipment') or 'Оборудование не указано'}")
    return "\n".join(lines)


def task_create_payload(values: dict[str, Any]) -> dict[str, Any]:
    """Проверяет поля формы и нормализует start_at к UTC Z."""
    name = str(values.get("name", "")).strip()
    task_file = str(values.get("task_file", "")).strip()
    start_text = str(values.get("start_at", "")).strip()
    duration = values.get("duration_s")
    if not name or not task_file or not start_text:
        raise ValueError("Заполните name, task_file и start_at")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise ValueError("duration_s обязано быть числом")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration_s обязано быть положительным")
    try:
        started = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("start_at должен быть ISO 8601") from exc
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return {
        "name": name,
        "task_file": task_file,
        "start_at": started.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration_s": float(duration),
        "equipment": str(values.get("equipment", "")).strip(),
    }


class SchedulerWindowsApp(AnalyticsWindowsApp):
    """Добавляет независимый opt-in глобального TaskScheduler."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.task_scheduler_var = self.tk.StringVar(
            value=task_scheduler_text(None)
        )
        self.task_scheduler_button = self.tk.Button(
            self.root,
            text="Планировщик заданий…",
            command=self._open_task_scheduler,
        )
        self.task_scheduler_button.pack(side="top", fill="x", padx=10, pady=(0, 8))
        self.task_create_var = self.tk.StringVar(value="Новая ControlTask ещё не создана")
        self.task_create_label = self.tk.Label(
            self.root,
            textvariable=self.task_create_var,
            wraplength=1100,
            justify="left",
        )
        self.task_create_label.pack(side="top", fill="x", padx=10, pady=(0, 8))
        self.task_create_button = self.tk.Button(
            self.root,
            text="Новая ControlTask…",
            command=self._open_task_create,
        )
        self.task_create_button.pack(side="top", fill="x", padx=10, pady=(0, 8))
        self.task_cancel_var = self.tk.StringVar(value="Queued ControlTask не отменена")
        self.task_cancel_label = self.tk.Label(
            self.root,
            textvariable=self.task_cancel_var,
            wraplength=1100,
            justify="left",
        )
        self.task_cancel_label.pack(side="top", fill="x", padx=10, pady=(0, 8))
        self.task_cancel_button = self.tk.Button(
            self.root,
            text="Отменить queued задачу…",
            command=self._open_task_cancel,
        )
        self.task_cancel_button.pack(side="top", fill="x", padx=10, pady=(0, 8))

    def _apply_snapshot(self, snapshot) -> None:
        super()._apply_snapshot(snapshot)
        self.task_scheduler_var.set(
            task_scheduler_text(
                snapshot.task_scheduler, snapshot.task_scheduler_error
            )
        )

    def _render_controls(self) -> None:
        super()._render_controls()
        if hasattr(self, "task_scheduler_button"):
            self.task_scheduler_button.configure(
                state="normal"
                if self.client is not None and not self._action_running
                else "disabled"
            )
        if hasattr(self, "task_create_button"):
            self.task_create_button.configure(
                state="normal"
                if self.client is not None and not self._action_running
                else "disabled"
            )
        if hasattr(self, "task_cancel_button"):
            self.task_cancel_button.configure(
                state="normal"
                if self.client is not None and not self._action_running
                else "disabled"
            )

    def _open_task_scheduler(self) -> None:
        status = self.snapshot.task_scheduler or {}
        window, frame, _error_var = self._new_form("Планировщик заданий")
        self.ttk.Label(
            frame,
            text=task_scheduler_text(status, self.snapshot.task_scheduler_error),
            wraplength=560,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=6)
        self.ttk.Label(
            frame,
            text=(
                "Глобальный runtime-only scheduler выполняет существующую TaskBook. "
                "После перезапуска требуется новое явное включение. Эта форма не "
                "создаёт задания, измерения и не открывает приборные порты."
            ),
            wraplength=560,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        list_frame = self.ttk.Frame(frame)
        list_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=6)
        task_text = self.tk.Text(
            list_frame,
            width=70,
            height=9,
            wrap="word",
            exportselection=False,
        )
        scrollbar = self.ttk.Scrollbar(
            list_frame, orient="vertical", command=task_text.yview
        )
        task_text.configure(yscrollcommand=scrollbar.set)
        task_text.insert(
            "1.0",
            task_scheduler_tasks_text(
                self.snapshot.task_scheduler_tasks,
                self.snapshot.task_scheduler_tasks_error,
            ),
        )
        task_text.configure(state="disabled")
        task_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        buttons = self.ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self.ttk.Button(
            buttons,
            text="Включить scheduler",
            command=lambda: self._configure_task_scheduler(True, window),
        ).pack(side="left")
        self.ttk.Button(
            buttons,
            text="Выключить",
            command=lambda: self._configure_task_scheduler(False, window),
        ).pack(side="left", padx=(8, 0))
        self.ttk.Button(
            buttons, text="Закрыть", command=lambda: self._destroy_form(window)
        ).pack(side="left", padx=(8, 0))

    def _open_task_create(self) -> None:
        window, frame, _error_var = self._new_form("Новая ControlTask")
        values = {
            "name": self.tk.StringVar(),
            "task_file": self.tk.StringVar(),
            "start_at": self.tk.StringVar(),
            "duration_s": self.tk.StringVar(value="60"),
            "equipment": self.tk.StringVar(),
        }
        fields = (
            ("name", "Имя"),
            ("task_file", "task_file"),
            ("start_at", "Начало ISO 8601"),
            ("duration_s", "Длительность, с"),
            ("equipment", "Оборудование"),
        )
        for row, (key, label) in enumerate(fields):
            self._form_entry(frame, row, label, values[key])
        self.ttk.Label(
            frame,
            text="status/outcome назначает сервер; локальные пути в ответ не включаются.",
            wraplength=560,
        ).grid(row=len(fields), column=0, columnspan=2, sticky="ew", pady=6)
        self.ttk.Button(
            frame,
            text="Создать задачу",
            command=lambda: self._create_task(window, values),
        ).grid(row=len(fields) + 1, column=0, sticky="w", pady=(10, 4))
        self.ttk.Button(
            frame, text="Закрыть", command=lambda: self._destroy_form(window)
        ).grid(row=len(fields) + 1, column=1, sticky="e", pady=(10, 4))

    def _create_task(self, window: Any, values: dict[str, Any]) -> None:
        client = self.client
        if client is None:
            return
        try:
            payload = task_create_payload({
                key: value.get() for key, value in values.items()
            })
        except ValueError as exc:
            self.task_create_var.set(str(exc))
            return
        self._destroy_form(window)
        self.task_create_var.set("ControlTask создаётся через API…")
        self._submit_action(
            "Создание ControlTask",
            lambda: client.create_task(payload),
            self._show_created_task,
        )

    def _show_created_task(self, created: dict[str, Any]) -> None:
        self.task_create_var.set(
            f"Создано: {created.get('name')} · {created.get('status')} · "
            f"{created.get('start_at')}"
        )

    def _open_task_cancel(self) -> None:
        queued = [
            task for task in self.snapshot.task_scheduler_tasks
            if task.get("status") == "В очереди на выполнение"
        ]
        if not queued:
            self.task_cancel_var.set("Нет задач в очереди; running не отменяется этой формой")
            return
        window, frame, _error_var = self._new_form("Отменить queued задачу")
        selected = self.tk.StringVar(value=str(queued[0].get("name", "")))
        self.ttk.Label(frame, text="Задача").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.ttk.Combobox(
            frame,
            textvariable=selected,
            values=[str(task.get("name", "")) for task in queued],
            state="readonly",
            width=56,
        ).grid(row=0, column=1, sticky="ew", pady=4)
        self.ttk.Label(
            frame,
            text="Отменяется только queued-задача; приборные потоки не останавливаются.",
            wraplength=560,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        self.ttk.Button(
            frame,
            text="Отменить задачу",
            command=lambda: self._cancel_task(window, selected.get()),
        ).grid(row=2, column=0, sticky="w", pady=(10, 4))
        self.ttk.Button(
            frame, text="Закрыть", command=lambda: self._destroy_form(window)
        ).grid(row=2, column=1, sticky="e", pady=(10, 4))

    def _cancel_task(self, window: Any, name: str) -> None:
        client = self.client
        if client is None or not name:
            return
        self._destroy_form(window)
        self.task_cancel_var.set(f"Отмена {name} через API…")
        self._submit_action(
            "Отмена queued ControlTask",
            lambda: client.cancel_task(name),
            self._show_cancelled_task,
        )

    def _show_cancelled_task(self, cancelled: dict[str, Any]) -> None:
        self.task_cancel_var.set(
            f"Отменено: {cancelled.get('name')} · {cancelled.get('status')} · "
            f"{cancelled.get('outcome')}"
        )

    def _configure_task_scheduler(self, enabled: bool, window: Any) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled должен быть JSON boolean")
        client = self.client
        self._destroy_form(window)
        self._submit_action(
            "Явное включение scheduler" if enabled else "Выключение scheduler",
            lambda: client.configure_task_scheduler(enabled),
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Windows GUI-клиент OpenRadioInspector")
    parser.add_argument("--server", default="http://127.0.0.1:8770", help="Базовый URL общего ядра")
    args = parser.parse_args(argv)
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print("Для Windows GUI требуется Python с компонентом Tkinter.", file=sys.stderr)
        return 2
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Графический дисплей недоступен: {exc}", file=sys.stderr)
        return 2
    app = SchedulerWindowsApp(root, tk, ttk)
    app.server_var.set(args.server)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
