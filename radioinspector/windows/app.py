"""Tkinter-оболочка Windows-клиента; вся измерительная логика остаётся в ядре."""

from __future__ import annotations

import argparse
import queue
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from .client import PlatformClient, PlatformClientError, PlatformSnapshot, validate_tune


def session_label(session: dict[str, Any]) -> str:
    return f"{session.get('name', 'Без имени')} · {session.get('state', '?')} · {session.get('id', '?')}"


def worker_row(
    stream: dict[str, Any], worker: Optional[dict[str, Any]]
) -> tuple[str, ...]:
    worker = worker or {}
    return (
        str(stream.get("device", "?")),
        str(stream.get("role", "?")),
        str(worker.get("state", stream.get("state", "disconnected"))),
        str(stream.get("level_unit", "?")),
        str(worker.get("measurements", 0)),
        str(worker.get("last_error") or "—"),
    )


def candidate_row(candidate: dict[str, Any]) -> tuple[str, ...]:
    level = candidate.get("peak_level")
    level_text = "—" if level is None else f"{level:g} {candidate.get('level_unit', '')}"
    return (
        str(candidate.get("state", "?")),
        str(candidate.get("center_frequency_hz", "?")),
        str(candidate.get("bandwidth_hz") or "—"),
        level_text,
        str(candidate.get("id", "?")),
    )


def investigation_row(investigation: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(investigation.get("state", "?")),
        str(investigation.get("priority", 0)),
        str(investigation.get("target_frequency_hz", "?")),
        str(investigation.get("mode") or "—"),
        str(investigation.get("target_stream_id", "?")),
    )


def policy_text(status: Optional[dict[str, Any]]) -> str:
    if not status:
        return "Состояние не получено"
    policy = status.get("policy", {})
    runtime = status.get("runtime", {})
    enabled = "включено" if policy.get("enabled") else "выключено"
    error = runtime.get("last_error")
    suffix = f" · ошибка: {error}" if error else ""
    return f"{enabled} · {runtime.get('last_result', '?')} · процесс: runtime-only{suffix}"


class WindowsApp:
    """Тонкий UI: фоновые HTTP-запросы не обращаются к Tk напрямую."""

    def __init__(
        self,
        root: Any,
        tk: Any,
        ttk: Any,
        *,
        client_factory: Callable[..., PlatformClient] = PlatformClient,
        refresh_ms: int = 2000,
    ) -> None:
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.client_factory = client_factory
        self.refresh_ms = refresh_ms
        self.client: Optional[PlatformClient] = None
        self.snapshot = PlatformSnapshot({}, {}, [])
        self.session_id: Optional[str] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="windows-api")
        self._messages: queue.Queue[tuple[str, Future, int, str]] = queue.Queue()
        self._revision = 0
        self._refresh_running = False
        self._action_running = False
        self._closed = False
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(0, self._drain_messages)
        self.root.after(self.refresh_ms, self._poll)

    def _build(self) -> None:
        ttk = self.ttk
        self.root.title("OpenRadioInspector — Windows")
        self.root.geometry("1180x800")
        self.root.minsize(940, 640)
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        connection = ttk.LabelFrame(outer, text="Общее ядро /api/v1", padding=8)
        connection.grid(row=0, column=0, sticky="ew")
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="Адрес сервера").grid(row=0, column=0, padx=(0, 6))
        self.server_var = self.tk.StringVar(value="http://127.0.0.1:8770")
        ttk.Entry(connection, textvariable=self.server_var).grid(row=0, column=1, sticky="ew")
        self.connect_button = ttk.Button(connection, text="Подключиться", command=self.connect)
        self.connect_button.grid(row=0, column=2, padx=(6, 0))
        self.refresh_button = ttk.Button(connection, text="Обновить", command=self.refresh)
        self.refresh_button.grid(row=0, column=3, padx=(6, 0))
        self.connection_var = self.tk.StringVar(value="Не подключено. Приборные порты открывает только сервер.")
        ttk.Label(connection, textvariable=self.connection_var, wraplength=1050).grid(
            row=1, column=0, columnspan=4, sticky="ew", pady=(7, 0)
        )

        sessions = ttk.LabelFrame(outer, text="Сеанс и независимые исполнители", padding=8)
        sessions.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        sessions.columnconfigure(1, weight=1)
        ttk.Label(sessions, text="Сеанс").grid(row=0, column=0, padx=(0, 6))
        self.session_box = ttk.Combobox(sessions, state="readonly")
        self.session_box.grid(row=0, column=1, columnspan=5, sticky="ew")
        self.session_box.bind("<<ComboboxSelected>>", self._select_session)
        self.session_start = ttk.Button(sessions, text="Запустить сеанс", command=lambda: self._session_action("start"))
        self.session_start.grid(row=1, column=0, pady=(8, 0), padx=(0, 6), sticky="ew")
        self.session_stop = ttk.Button(sessions, text="Остановить сеанс", command=lambda: self._session_action("stop"))
        self.session_stop.grid(row=1, column=1, pady=(8, 0), padx=(0, 6), sticky="w")
        self.workers_start = ttk.Button(sessions, text="Запустить все потоки", command=lambda: self._workers_action("start"))
        self.workers_start.grid(row=1, column=2, pady=(8, 0), padx=(0, 6))
        self.workers_stop = ttk.Button(sessions, text="Остановить все потоки", command=lambda: self._workers_action("stop"))
        self.workers_stop.grid(row=1, column=3, pady=(8, 0), padx=(0, 6))
        self.stream_start = ttk.Button(sessions, text="Запустить выбранный", command=lambda: self._stream_action("start"))
        self.stream_start.grid(row=1, column=4, pady=(8, 0), padx=(0, 6))
        self.stream_stop = ttk.Button(sessions, text="Остановить выбранный", command=lambda: self._stream_action("stop"))
        self.stream_stop.grid(row=1, column=5, pady=(8, 0))

        tune = ttk.LabelFrame(outer, text="Перестройка выбранного receiver через его очередь", padding=8)
        tune.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(tune, text="Частота, целые Гц").grid(row=0, column=0, padx=(0, 6))
        self.frequency_var = self.tk.StringVar()
        ttk.Entry(tune, textvariable=self.frequency_var, width=22).grid(row=0, column=1, padx=(0, 12))
        ttk.Label(tune, text="Режим").grid(row=0, column=2, padx=(0, 6))
        self.mode_var = self.tk.StringVar()
        ttk.Entry(tune, textvariable=self.mode_var, width=16).grid(row=0, column=3, padx=(0, 12))
        self.tune_button = ttk.Button(tune, text="Настроить receiver", command=self._tune)
        self.tune_button.grid(row=0, column=4)
        ttk.Label(tune, text="tinySA при этом не останавливается; прежняя частота автоматически не восстанавливается.").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(7, 0)
        )

        policies = ttk.Frame(outer)
        policies.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        policies.columnconfigure((0, 1), weight=1)
        self.detection_var = self.tk.StringVar(value="Обнаружение: состояние не получено")
        self.handoff_var = self.tk.StringVar(value="Handoff: состояние не получено")
        ttk.Label(policies, textvariable=self.detection_var, wraplength=550).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(policies, textvariable=self.handoff_var, wraplength=550).grid(row=0, column=1, sticky="ew")

        notebook = ttk.Notebook(outer)
        notebook.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        self.streams_tree = self._tree(
            notebook,
            ("device", "role", "state", "unit", "measurements", "error"),
            ("Прибор", "Роль", "Состояние", "Единица", "Измерения", "Последняя ошибка"),
            (170, 140, 110, 120, 90, 330),
        )
        notebook.add(self.streams_tree.master, text="Потоки")
        self.candidates_tree = self._tree(
            notebook,
            ("state", "frequency", "bandwidth", "level", "id"),
            ("Состояние", "Частота, Гц", "Полоса, Гц", "Исходный пик", "ID"),
            (110, 150, 130, 150, 360),
        )
        notebook.add(self.candidates_tree.master, text="Кандидаты")
        self.investigations_tree = self._tree(
            notebook,
            ("state", "priority", "frequency", "mode", "receiver"),
            ("Состояние", "Приоритет", "Частота, Гц", "Режим", "Receiver ID"),
            (110, 90, 160, 110, 360),
        )
        notebook.add(self.investigations_tree.master, text="Исследования")
        self.streams_tree.bind("<<TreeviewSelect>>", self._select_stream)
        self.action_var = self.tk.StringVar(value="Команды выполняются на сервере; GUI не пишет в архив.")
        ttk.Label(outer, textvariable=self.action_var, wraplength=1100).grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self._render_controls()

    def _tree(self, parent: Any, columns: tuple[str, ...], headings: tuple[str, ...], widths: tuple[int, ...]) -> Any:
        frame = self.ttk.Frame(parent)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = self.ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=70, stretch=True)
        scroll = self.ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        return tree

    def connect(self) -> None:
        try:
            self.client = self.client_factory(self.server_var.get())
        except (ValueError, PlatformClientError) as exc:
            self.connection_var.set(str(exc))
            return
        self.session_id = None
        self._revision += 1
        self.connection_var.set("Запрашиваются health, capabilities и сеансы…")
        self.refresh()

    def refresh(self) -> None:
        if self.client is None or self._refresh_running or self._closed:
            return
        self._refresh_running = True
        revision = self._revision
        future = self._executor.submit(self.client.snapshot, self.session_id)
        future.add_done_callback(
            lambda done: self._messages.put(("snapshot", done, revision, ""))
        )

    def _poll(self) -> None:
        if not self._closed:
            self.refresh()
            self.root.after(self.refresh_ms, self._poll)

    def _submit_action(self, description: str, operation: Callable[[], Any]) -> None:
        if self.client is None or self._action_running or self._closed:
            return
        self._action_running = True
        self.action_var.set(f"{description}…")
        self._render_controls()
        revision = self._revision
        future = self._executor.submit(operation)
        future.add_done_callback(
            lambda done: self._messages.put(("action", done, revision, description))
        )

    def _drain_messages(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, future, revision, description = self._messages.get_nowait()
                if revision != self._revision:
                    if kind == "snapshot":
                        self._refresh_running = False
                    else:
                        self._action_running = False
                    self._render_controls()
                    self.refresh()
                    continue
                try:
                    result = future.result()
                except Exception as exc:
                    if kind == "snapshot":
                        self._refresh_running = False
                        self.connection_var.set(f"Ошибка обновления: {exc}")
                    else:
                        self._action_running = False
                        self.action_var.set(f"{description}: {exc}")
                    self._render_controls()
                    continue
                if kind == "snapshot":
                    self._refresh_running = False
                    self._apply_snapshot(result)
                else:
                    self._action_running = False
                    self.action_var.set(f"{description}: выполнено сервером.")
                    self._render_controls()
                    self.refresh()
        except queue.Empty:
            pass
        self.root.after(100, self._drain_messages)

    def _apply_snapshot(self, snapshot: PlatformSnapshot) -> None:
        self.snapshot = snapshot
        selected_exists = any(item.get("id") == self.session_id for item in snapshot.sessions)
        if self.session_id is not None and not selected_exists:
            self.session_id = None
        labels = [session_label(item) for item in snapshot.sessions]
        self.session_box.configure(values=labels)
        needs_details = False
        if self.session_id is not None:
            index = next(i for i, item in enumerate(snapshot.sessions) if item.get("id") == self.session_id)
            self.session_box.current(index)
        elif snapshot.sessions:
            self.session_box.current(0)
            self.session_id = snapshot.sessions[0].get("id")
            needs_details = snapshot.session is None
        else:
            self.session_box.set("")
        health = snapshot.health
        self.connection_var.set(
            f"Подключено · сеансов {health.get('sessions', len(snapshot.sessions))} · "
            f"работает {health.get('running_sessions', '?')} · событие {health.get('last_event_id', '?')}"
        )
        streams = (snapshot.session or {}).get("streams", [])
        workers = {item.get("stream_id"): item for item in snapshot.workers}
        selected_stream = self.selected_stream_id()
        self.streams_tree.delete(*self.streams_tree.get_children())
        for stream in streams:
            stream_id = str(stream.get("id"))
            self.streams_tree.insert("", "end", iid=stream_id, values=worker_row(stream, workers.get(stream_id)))
        if selected_stream and self.streams_tree.exists(selected_stream):
            self.streams_tree.selection_set(selected_stream)
        self._fill(self.candidates_tree, snapshot.candidates, candidate_row)
        self._fill(self.investigations_tree, snapshot.investigations, investigation_row)
        self.detection_var.set(f"Обнаружение: {policy_text(snapshot.automatic_detection)}")
        self.handoff_var.set(f"Handoff: {policy_text(snapshot.automatic_handoff)}")
        self._render_controls()
        if needs_details:
            self.refresh()

    @staticmethod
    def _fill(tree: Any, records: list[dict[str, Any]], formatter: Callable[[dict[str, Any]], tuple[str, ...]]) -> None:
        tree.delete(*tree.get_children())
        for index, record in enumerate(records):
            tree.insert("", "end", iid=str(index), values=formatter(record))

    def _select_session(self, _event: Any = None) -> None:
        index = self.session_box.current()
        if not 0 <= index < len(self.snapshot.sessions):
            return
        selected = self.snapshot.sessions[index].get("id")
        if selected == self.session_id:
            return
        self.session_id = selected
        self._revision += 1
        self.refresh()

    def selected_stream_id(self) -> Optional[str]:
        selection = self.streams_tree.selection()
        return str(selection[0]) if selection else None

    def _selected_stream(self) -> Optional[dict[str, Any]]:
        stream_id = self.selected_stream_id()
        return next(
            (item for item in (self.snapshot.session or {}).get("streams", []) if item.get("id") == stream_id),
            None,
        )

    def _select_stream(self, _event: Any = None) -> None:
        self._render_controls()
        stream = self._selected_stream()
        if not stream:
            return
        worker = next((item for item in self.snapshot.workers if item.get("stream_id") == stream.get("id")), {})
        current = worker.get("current") or stream.get("settings") or {}
        frequency = current.get("frequency_hz")
        if frequency:
            self.frequency_var.set(str(frequency))
        if current.get("mode"):
            self.mode_var.set(str(current["mode"]))

    def _session_action(self, action: str) -> None:
        if not self._require_session():
            return
        operation = self.client.start_session if action == "start" else self.client.stop_session
        session_id = self.session_id
        self._submit_action(
            "Запуск сеанса" if action == "start" else "Остановка сеанса",
            lambda: operation(session_id),
        )

    def _workers_action(self, action: str) -> None:
        if not self._require_session():
            return
        operation = self.client.start_workers if action == "start" else self.client.stop_workers
        session_id = self.session_id
        self._submit_action(
            "Запуск всех потоков" if action == "start" else "Остановка всех потоков",
            lambda: operation(session_id),
        )

    def _stream_action(self, action: str) -> None:
        stream_id = self.selected_stream_id()
        if not self._require_session() or not stream_id:
            self.action_var.set("Сначала выберите поток.")
            return
        operation = self.client.start_stream if action == "start" else self.client.stop_stream
        session_id = self.session_id
        self._submit_action(
            "Запуск выбранного потока" if action == "start" else "Остановка выбранного потока",
            lambda: operation(session_id, stream_id),
        )

    def _tune(self) -> None:
        stream = self._selected_stream()
        if not self._require_session() or not stream:
            self.action_var.set("Сначала выберите receiver.")
            return
        if stream.get("role") != "receiver":
            self.action_var.set("Перестройка доступна только потоку receiver; tinySA не изменяется.")
            return
        try:
            frequency, mode = validate_tune(int(self.frequency_var.get()), self.mode_var.get())
        except (ValueError, TypeError) as exc:
            self.action_var.set(f"Некорректная команда: {exc}")
            return
        stream_id = stream["id"]
        session_id = self.session_id
        client = self.client
        self._submit_action(
            f"Перестройка receiver на {frequency} Гц ({mode})",
            lambda: client.tune(session_id, stream_id, frequency_hz=frequency, mode=mode),
        )

    def _require_session(self) -> bool:
        if self.client is not None and self.session_id:
            return True
        self.action_var.set("Сначала подключитесь и выберите сеанс.")
        return False

    def _render_controls(self) -> None:
        connected = self.client is not None and not self._action_running
        selected = connected and self.session_id is not None
        for button in (self.session_start, self.session_stop, self.workers_start, self.workers_stop):
            button.configure(state="normal" if selected else "disabled")
        stream_selected = selected and self.selected_stream_id() is not None
        for button in (self.stream_start, self.stream_stop, self.tune_button):
            button.configure(state="normal" if stream_selected else "disabled")
        self.refresh_button.configure(state="normal" if self.client is not None else "disabled")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


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
    app = WindowsApp(root, tk, ttk)
    app.server_var.set(args.server)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
