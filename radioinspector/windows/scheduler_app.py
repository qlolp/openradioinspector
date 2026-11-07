"""Windows-панель глобального TaskScheduler без привязки к сеансу."""

from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from .completion_app import CompletionWindowsApp


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


class SchedulerWindowsApp(CompletionWindowsApp):
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
        buttons = self.ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 4))
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
