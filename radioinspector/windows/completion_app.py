"""Windows-форма automatic completion поверх существующего WindowsApp."""

from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from .app import WindowsApp, policy_text
from .workflows import automatic_completion_payload


class CompletionWindowsApp(WindowsApp):
    """Добавляет opt-in completion, не меняя detector и handoff."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._build_completion_controls()

    def _build_completion_controls(self) -> None:
        policies = self.detection_policy_button.master
        self.completion_var = self.tk.StringVar(
            value="Завершение: состояние не получено"
        )
        self.completion_status_label = self.ttk.Label(
            policies, textvariable=self.completion_var, wraplength=1000
        )
        self.completion_status_label.grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0)
        )
        self.completion_policy_button = self.ttk.Button(
            policies,
            text="Настроить завершение…",
            command=lambda: self._open_policy_form("completion"),
            state="disabled",
        )
        self.completion_policy_button.grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=(6, 0)
        )

    def _apply_snapshot(self, snapshot) -> None:
        super()._apply_snapshot(snapshot)
        self.completion_var.set(
            f"Завершение: {policy_text(snapshot.automatic_completion)}"
        )

    def _render_controls(self) -> None:
        super()._render_controls()
        if hasattr(self, "completion_policy_button"):
            self.completion_policy_button.configure(
                state="normal"
                if self.client is not None
                and self.session_id is not None
                and not self._action_running
                else "disabled"
            )

    def _open_policy_form(self, kind: str) -> None:
        if kind != "completion":
            return super()._open_policy_form(kind)
        if not self._require_session():
            return
        status = self.snapshot.automatic_completion or {}
        policy = dict(status.get("policy") or {})
        defaults = {
            "interval_seconds": 1.0,
            "minimum_listening_seconds": 0.0,
            "minimum_observations": 1,
            "receiver_stream_id": None,
        }
        values = {**defaults, **policy}
        window, frame, error_var = self._new_form("Автоматическое завершение")
        fields = (
            ("interval_seconds", "Период проверки, с"),
            ("minimum_listening_seconds", "Минимум прослушивания, с"),
            ("minimum_observations", "Минимум наблюдений"),
        )
        variables = {
            name: self.tk.StringVar(value=str(values.get(name, "")))
            for name, _label in fields
        }
        for row, (name, label) in enumerate(fields):
            self._form_entry(frame, row, label, variables[name])
        row = len(fields)
        stream_var = self.tk.StringVar(value=str(values.get("receiver_stream_id") or ""))
        streams = [
            item
            for item in (self.snapshot.session or {}).get("streams", [])
            if item.get("role") == "receiver"
        ]
        self.ttk.Label(frame, text="Receiver (пусто — любой)").grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.ttk.Combobox(
            frame,
            textvariable=stream_var,
            values=[""] + [str(item["id"]) for item in streams],
            state="readonly",
        ).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        self.ttk.Label(
            frame,
            text=(
                "Завершаются только listening/decoding после минимального времени "
                "и нужного числа наблюдений; automatic fail/cancel нет. "
                "После перезапуска сервера требуется новое явное включение."
            ),
            wraplength=560,
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
        client = self.client
        session_id = self.session_id

        def make_payload(enabled: bool) -> dict[str, Any]:
            parameters = {
                name: variable.get() for name, variable in variables.items()
            }
            parameters["receiver_stream_id"] = stream_var.get()
            return automatic_completion_payload(enabled=enabled, **parameters)

        def submit(enabled: bool) -> None:
            if enabled and (self.snapshot.session or {}).get("state") != "running":
                error_var.set(
                    "Автоматическое завершение можно включить только в работающем сеансе."
                )
                return
            try:
                payload = make_payload(enabled)
            except (TypeError, ValueError) as exc:
                error_var.set(str(exc))
                return
            self._destroy_form(window)
            description = (
                "Явное включение завершения"
                if enabled
                else "Сохранение выключенной политики завершения"
            )
            self._submit_action(
                description,
                lambda: client.configure_automatic_completion(session_id, payload),
            )

        row += 1
        buttons = self.ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        enabled = bool(policy.get("enabled"))
        if enabled:
            self.ttk.Button(
                buttons, text="Сохранить включённой", command=lambda: submit(True)
            ).pack(side="left")
            self.ttk.Button(
                buttons,
                text="Выключить",
                command=lambda: (
                    self._destroy_form(window),
                    self._disable_policy("completion"),
                ),
            ).pack(side="left", padx=(8, 0))
        else:
            self.ttk.Button(
                buttons, text="Сохранить выключенной", command=lambda: submit(False)
            ).pack(side="left")
            self.ttk.Button(
                buttons, text="Включить явно", command=lambda: submit(True)
            ).pack(side="left", padx=(8, 0))
        self.ttk.Button(
            buttons, text="Отмена", command=lambda: self._destroy_form(window)
        ).pack(side="left", padx=(8, 0))
        row += 1
        self.ttk.Label(
            frame, textvariable=error_var, foreground="#b3261e", wraplength=560
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)

    def _disable_policy(self, kind: str) -> None:
        if kind != "completion":
            return super()._disable_policy(kind)
        if not self._require_session():
            return
        client = self.client
        session_id = self.session_id
        self._submit_action(
            "Выключение завершения; другие политики не изменяются",
            lambda: client.configure_automatic_completion(
                session_id, {"enabled": False}
            ),
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
    app = CompletionWindowsApp(root, tk, ttk)
    app.server_var.set(args.server)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
