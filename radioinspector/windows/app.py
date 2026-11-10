"""Tkinter-оболочка Windows-клиента; вся измерительная логика остаётся в ядре."""

from __future__ import annotations

import argparse
import queue
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from threading import Event, Lock
from typing import Any, Callable, Optional

from .archive_capacity import CAPACITY_NOTE, archive_capacity_color, archive_capacity_text
from .client import PlatformClient, PlatformClientError, PlatformSnapshot, validate_tune
from .spectrum import peak_of, sampled_indices, waterfall_color, waterfall_levels
from .tk_timers import TkTimerOwner
from .spectrum_view import SpectrumView, fit_bounds, fraction
from .workflows import (
    automatic_detection_payload,
    automatic_handoff_payload,
    candidate_payload,
    investigation_payload,
    session_payload,
)


def _publish_message(messages, lock, closed, kind, revision, description, future) -> None:
    """Worker completion owns only publication state, never a GUI/Tcl graph."""
    with lock:
        if not closed.is_set():
            messages.put((kind, future, revision, description))


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
        str(investigation.get("id", "?")),
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


class WindowsApp(TkTimerOwner):
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
        self._last_selected_stream_id: Optional[str] = None
        self.spectrum_view = SpectrumView()
        self._spectrum_available = True
        self._waterfall_photo: Any = None
        self._spectrum_draw_key: Any = None
        self._waterfall_draw_key: Any = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="windows-api")
        self._messages: queue.Queue[tuple[str, Future, int, str]] = queue.Queue()
        self._message_lock = Lock()
        self._messages_closed = Event()
        self._revision = 0
        self._refresh_running = False
        self._action_running = False
        self._action_callbacks: dict[Future, Callable[[Any], None]] = {}
        self._project_action_clients: dict[Future, PlatformClient] = {}
        self._closed = False
        self._init_timers(root, self._shutdown)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._schedule_after(self.root, "messages", 0, self._drain_messages)
        self._schedule_after(self.root, "poll", self.refresh_ms, self._poll)

    def _build(self) -> None:
        ttk = self.ttk
        self.root.title("OpenRadioInspector — Windows")
        self.root.geometry("1280x900")
        self.root.minsize(1040, 720)
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

        self.archive_capacity_var = self.tk.StringVar(value=archive_capacity_text(None))
        self.archive_capacity_label = ttk.Label(
            connection, textvariable=self.archive_capacity_var, wraplength=1050
        )
        self.archive_capacity_label.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(7, 0))
        ttk.Label(connection, text=CAPACITY_NOTE, wraplength=1050).grid(
            row=3, column=0, columnspan=4, sticky="ew"
        )

        sessions = ttk.LabelFrame(outer, text="Сеанс и независимые исполнители", padding=8)
        sessions.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        sessions.columnconfigure(1, weight=1)
        ttk.Label(sessions, text="Сеанс").grid(row=0, column=0, padx=(0, 6))
        self.session_box = ttk.Combobox(sessions, state="readonly")
        self.session_box.grid(row=0, column=1, columnspan=5, sticky="ew")
        self.session_box.bind("<<ComboboxSelected>>", self._select_session)
        self.session_create = ttk.Button(
            sessions, text="Новый сеанс…", command=self._open_session_form
        )
        self.session_create.grid(row=0, column=6, padx=(8, 0))
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
        self.detection_policy_button = ttk.Button(
            policies,
            text="Настроить обнаружение…",
            command=lambda: self._open_policy_form("detection"),
        )
        self.detection_policy_button.grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0)
        )
        self.handoff_policy_button = ttk.Button(
            policies,
            text="Настроить handoff…",
            command=lambda: self._open_policy_form("handoff"),
        )
        self.handoff_policy_button.grid(
            row=1, column=1, sticky="w", pady=(6, 0)
        )

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
        candidate_actions = ttk.Frame(self.candidates_tree.master)
        candidate_actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.peak_candidate_button = ttk.Button(
            candidate_actions, text="Кандидат из пика…",
            command=lambda: self._open_candidate_form(from_peak=True),
        )
        self.peak_candidate_button.pack(side="left")
        self.manual_candidate_button = ttk.Button(
            candidate_actions, text="Ручной кандидат…",
            command=lambda: self._open_candidate_form(from_peak=False),
        )
        self.manual_candidate_button.pack(side="left", padx=(6, 0))
        self.enqueue_button = ttk.Button(
            candidate_actions, text="Поставить в очередь…", command=self._open_enqueue_form
        )
        self.enqueue_button.pack(side="left", padx=(6, 0))
        self.dismiss_button = ttk.Button(
            candidate_actions, text="Отклонить", command=self._dismiss_candidate
        )
        self.dismiss_button.pack(side="left", padx=(6, 0))
        self.candidates_tree.bind("<<TreeviewSelect>>", lambda _event: self._render_controls())

        self.investigations_tree = self._tree(
            notebook,
            ("state", "priority", "frequency", "mode", "receiver", "id"),
            ("Состояние", "Приоритет", "Частота, Гц", "Режим", "Receiver ID", "ID"),
            (100, 80, 140, 90, 250, 300),
        )
        notebook.add(self.investigations_tree.master, text="Исследования")
        self.next_investigation_var = self.tk.StringVar(
            value="Следующее: очередь не получена"
        )
        ttk.Label(
            self.investigations_tree.master,
            textvariable=self.next_investigation_var,
            wraplength=1100,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        investigation_actions = ttk.Frame(self.investigations_tree.master)
        investigation_actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.start_next_button = ttk.Button(
            investigation_actions, text="Подтвердить следующий", command=self._start_next
        )
        self.start_next_button.pack(side="left")
        self.investigation_start = ttk.Button(
            investigation_actions, text="Запустить выбранное",
            command=lambda: self._investigation_action("start"),
        )
        self.investigation_start.pack(side="left", padx=(6, 0))
        self.investigation_complete = ttk.Button(
            investigation_actions, text="Завершить",
            command=lambda: self._investigation_action("complete"),
        )
        self.investigation_complete.pack(side="left", padx=(6, 0))
        self.investigation_cancel = ttk.Button(
            investigation_actions, text="Отменить",
            command=lambda: self._investigation_action("cancel"),
        )
        self.investigation_cancel.pack(side="left", padx=(6, 0))
        ttk.Label(investigation_actions, text="Причина сбоя").pack(side="left", padx=(12, 4))
        self.failure_var = self.tk.StringVar(value="Остановлено оператором через Windows GUI")
        ttk.Entry(investigation_actions, textvariable=self.failure_var, width=34).pack(side="left")
        self.investigation_fail = ttk.Button(
            investigation_actions, text="Зафиксировать сбой",
            command=lambda: self._investigation_action("fail"),
        )
        self.investigation_fail.pack(side="left", padx=(6, 0))
        self.investigations_tree.bind("<<TreeviewSelect>>", lambda _event: self._render_controls())
        spectrum_tab = ttk.Frame(notebook, padding=8)
        spectrum_tab.columnconfigure(0, weight=1)
        spectrum_tab.rowconfigure(1, weight=3)
        spectrum_tab.rowconfigure(3, weight=2)
        self.spectrum_info_var = self.tk.StringVar(
            value="Выберите поток spectrum_analyzer с исходными dBm на вкладке «Потоки»."
        )
        ttk.Label(spectrum_tab, textvariable=self.spectrum_info_var, wraplength=1050).grid(
            row=0, column=0, sticky="ew", pady=(0, 6)
        )
        self.spectrum_canvas = self.tk.Canvas(
            spectrum_tab, background="#07131c", height=260, highlightthickness=0
        )
        self.spectrum_canvas.grid(row=1, column=0, sticky="nsew")
        self.waterfall_info_var = self.tk.StringVar(value="Водопад: ожидание данных")
        ttk.Label(spectrum_tab, textvariable=self.waterfall_info_var, wraplength=1050).grid(
            row=2, column=0, sticky="ew", pady=(8, 6)
        )
        self.waterfall_canvas = self.tk.Canvas(
            spectrum_tab, background="#07131c", height=190, highlightthickness=0
        )
        self.waterfall_canvas.grid(row=3, column=0, sticky="nsew")
        self.spectrum_canvas.bind(
            "<Configure>", lambda _event: self._render_spectrum()
        )
        self.waterfall_canvas.bind(
            "<Configure>", lambda _event: self._render_spectrum()
        )
        controls = ttk.Frame(spectrum_tab)
        controls.grid(row=4, column=0, sticky="ew")
        self.pause_view_button = ttk.Button(controls, text="Pause view", command=self._toggle_spectrum_pause)
        self.pause_view_button.pack(side="left")
        self.fit_view_button = ttk.Button(controls, text="Fit", command=self._fit_spectrum)
        self.fit_view_button.pack(side="left")
        self.lower_view_var = self.tk.StringVar()
        self.upper_view_var = self.tk.StringVar()
        for label, variable in (("Lower dBm", self.lower_view_var), ("Upper dBm", self.upper_view_var)):
            ttk.Label(controls, text=label).pack(side="left")
            ttk.Entry(controls, textvariable=variable, width=14).pack(side="left")
        self.apply_view_button = ttk.Button(controls, text="Apply", command=self._apply_spectrum_bounds)
        self.apply_view_button.pack(side="left")
        self.view_status_var = self.tk.StringVar()
        ttk.Label(spectrum_tab, textvariable=self.view_status_var, wraplength=1050).grid(row=5, column=0, sticky="ew")
        notebook.add(spectrum_tab, text="Спектр и водопад")
        from .sweep_comparison import ComparisonPanel
        self.comparison_panel = ComparisonPanel(self, notebook)
        from .sweep_threshold import ThresholdPanel
        self.threshold_panel = ThresholdPanel(self, notebook)
        from .spectral_mask import SpectralMaskPanel
        self.mask_panel = SpectralMaskPanel(self, notebook)
        from .frequency_raster import FrequencyRasterPanel
        self.raster_panel = FrequencyRasterPanel(self, notebook)
        from .measurement_results import MeasurementResultPanel
        self.measurement_panel = MeasurementResultPanel(self, notebook)
        from .frequency_counter import FrequencyCounterPanel
        self.counter_panel = FrequencyCounterPanel(self, notebook)
        from .dvb_parameters import DvbEvaluationPanel
        self.dvb_panel = DvbEvaluationPanel(self, notebook)
        from .phonogram import PhonogramPanel
        self.phonogram_panel = PhonogramPanel(self, notebook)
        from .signal_list import SignalListPanel
        self.signal_list_panel = SignalListPanel(self, notebook)
        from .measurement_map import MeasurementMapPanel
        self.map_panel = MeasurementMapPanel(self, notebook)
        from .audio_stream import AudioStreamPanel
        self.audio_stream_panel = AudioStreamPanel(self, notebook)
        from .tinysa import TinySAPanel
        self.tinysa_panel = TinySAPanel(self, notebook)
        from .sweep_average import AveragePanel
        self.average_panel = AveragePanel(self, notebook)
        from .sweep_three_thresholds import ThreeThresholdsPanel
        self.three_thresholds_panel = ThreeThresholdsPanel(self, notebook)
        from .sweep_context import SweepContextPanel
        self.sweep_context_panel = SweepContextPanel(self, notebook)
        from .sweep_quality import SweepQualityPanel
        self.sweep_quality_panel = SweepQualityPanel(self, notebook)
        from .frequency_rounding import FrequencyRoundingPanel
        self.frequency_rounding_panel = FrequencyRoundingPanel(self, notebook)
        from .monitoring import MonitoringPanel
        self.monitoring_panel = MonitoringPanel(self, notebook)
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
        if getattr(self, "_closed", False):
            return
        try:
            self.client = self.client_factory(self.server_var.get())
        except (ValueError, PlatformClientError) as exc:
            self.connection_var.set(str(exc))
            return
        self.session_id = None
        self._clear_spectrum("Подключение: ожидание данных")
        if hasattr(self, "comparison_panel"):
            self.comparison_panel.update()
        if hasattr(self, "threshold_panel"):
            self.threshold_panel.update()
        if hasattr(self, "mask_panel"):
            self.mask_panel.update()
        if hasattr(self, "raster_panel"):
            self.raster_panel.update()
        if hasattr(self, "measurement_panel"):
            self.measurement_panel.update()
        if hasattr(self, "counter_panel"):
            self.counter_panel.update()
        if hasattr(self, "dvb_panel"):
            self.dvb_panel.update()
        if hasattr(self, "phonogram_panel"):
            self.phonogram_panel.update()
        if hasattr(self, "signal_list_panel"):
            self.signal_list_panel.update()
        if hasattr(self, "map_panel"):
            self.map_panel.update()
        if hasattr(self, "audio_stream_panel"):
            self.audio_stream_panel.update()
        if hasattr(self, "three_thresholds_panel"):
            self.three_thresholds_panel.update()
        if hasattr(self, "sweep_context_panel"):
            self.sweep_context_panel.update()
        if hasattr(self, "sweep_quality_panel"):
            self.sweep_quality_panel.update()
        if hasattr(self, "frequency_rounding_panel"):
            self.frequency_rounding_panel.update()
        if hasattr(self, "average_panel"):
            self.average_panel.update()
        self._revision += 1
        self._show_archive_capacity(None)
        self.connection_var.set("Запрашиваются health, capabilities и сеансы…")
        self.refresh()

    def refresh(self) -> None:
        if self.client is None or self._refresh_running or self._closed:
            return
        self._refresh_running = True
        revision = self._revision
        client = self.client
        session_id = self.session_id
        spectrum_stream_id = self._spectrum_stream_id()
        future = self._executor.submit(
            client.snapshot,
            session_id,
            spectrum_stream_id=spectrum_stream_id,
        )
        future.add_done_callback(
            self._completion_callback("snapshot", revision, "")
        )

    def _poll(self) -> None:
        if not self._closed:
            self.refresh()
            self._schedule_after(self.root, "poll", self.refresh_ms, self._poll)

    def _queue_message(self, kind: str, future: Future, revision: int, description: str) -> None:
        _publish_message(self._messages, self._message_lock, self._messages_closed,
                         kind, revision, description, future)

    def _completion_callback(self, kind: str, revision: int, description: str):
        # Capturing self, even through a weakref that is later resolved, can
        # release the last GUI reference on a worker when its Future finishes.
        return partial(_publish_message, self._messages, self._message_lock,
                       self._messages_closed, kind, revision, description)

    def _submit_action(
        self,
        description: str,
        operation: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        *, project_scoped: bool = False, background: bool = False,
    ) -> None:
        if self.client is None or (self._action_running and not background) or self._closed:
            return
        if not background:
            self._action_running = True
            self.action_var.set(f"{description}…")
            self._render_controls()
        revision = self._revision
        future = self._executor.submit(operation)
        if project_scoped:
            self._project_action_clients[future] = self.client
        if on_success is not None:
            self._action_callbacks[future] = on_success
        future.add_done_callback(
            self._completion_callback("project_read" if background else "action", revision, description)
        )

    def _submit_project_action(self, description, operation, on_success=None) -> None:
        # Project context follows a connected core, independently of research session selection.
        self._submit_action(description, operation, on_success, project_scoped=True)

    def _submit_project_read(self,description,operation,on_success=None) -> None:
        self._submit_action(description,operation,on_success,project_scoped=True,background=True)

    def _drain_messages(self) -> None:
        if self._closed:
            return
        try:
            while not self._closed:
                kind, future, revision, description = self._messages.get_nowait()
                project_client = getattr(self, '_project_action_clients', {}).pop(future, None)
                same_project_core = project_client is not None and project_client is self.client
                if kind=='project_read':
                    callback=self._action_callbacks.pop(future,None)
                    if revision!=self._revision and not same_project_core:continue
                    try:result=future.result()
                    except Exception:continue
                    if callback is not None:callback(result)
                    continue
                if revision != self._revision and not same_project_core:
                    if kind == "snapshot":
                        self._refresh_running = False
                    else:
                        self._action_running = False
                        self._action_callbacks.pop(future, None)
                    self._render_controls()
                    self.refresh()
                    continue
                try:
                    result = future.result()
                except Exception as exc:
                    if kind == "snapshot":
                        self._refresh_running = False
                        self.connection_var.set(f"Ошибка обновления: {exc}")
                        self._spectrum_available = False
                        if hasattr(self, "spectrum_view"):
                            self._clear_spectrum("Спектр недоступен: ошибка соединения")
                        if hasattr(self, "average_panel"):
                            self.average_panel.clear()
                        if hasattr(self, "three_thresholds_panel"):
                            self.three_thresholds_panel.clear()
                        if hasattr(self, "sweep_context_panel"):
                            self.sweep_context_panel.disconnect()
                        if hasattr(self, "sweep_quality_panel"):
                            self.sweep_quality_panel.disconnect()
                        if hasattr(self, "frequency_rounding_panel"):
                            self.frequency_rounding_panel.disconnect()
                        self._show_archive_capacity(None, str(exc))
                    else:
                        self._action_running = False
                        self._action_callbacks.pop(future, None)
                        self.action_var.set(f"{description}: {exc}")
                    self._render_controls()
                    continue
                if kind == "snapshot":
                    self._refresh_running = False
                    self._apply_snapshot(result)
                    if self._closed:
                        return
                    if hasattr(self, "sweep_context_panel"):
                        self.sweep_context_panel.snapshot_refreshed()
                    if hasattr(self, "sweep_quality_panel"):
                        self.sweep_quality_panel.snapshot_refreshed()
                    if hasattr(self, "frequency_rounding_panel"):
                        self.frequency_rounding_panel.snapshot_refreshed()
                else:
                    self._action_running = False
                    callback = self._action_callbacks.pop(future, None)
                    if callback is not None:
                        callback(result)
                    if self._closed:
                        return
                    self.action_var.set(f"{description}: выполнено сервером.")
                    self._render_controls()
                    self.refresh()
        except queue.Empty:
            pass
        self._schedule_after(self.root, "messages", 100, self._drain_messages)

    def _show_archive_capacity(self, status, error=None) -> None:
        self.archive_capacity_var.set(archive_capacity_text(status, error))
        self.archive_capacity_label.configure(foreground=archive_capacity_color(status, error))

    def _apply_snapshot(self, snapshot: PlatformSnapshot) -> None:
        self._spectrum_available = True
        self.snapshot = snapshot
        self._show_archive_capacity(snapshot.archive_capacity, snapshot.archive_capacity_error)
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
        elif streams:
            preferred = next(
                (
                    item
                    for item in streams
                    if item.get("role") == "spectrum_analyzer"
                    and item.get("level_unit") == "dBm"
                ),
                streams[0],
            )
            self.streams_tree.selection_set(str(preferred.get("id")))
        # Восстановление выделения не является новым пользовательским выбором.
        self._last_selected_stream_id = self.selected_stream_id()
        self._fill(self.candidates_tree, snapshot.candidates, candidate_row)
        self._fill(self.investigations_tree, snapshot.investigations, investigation_row)
        next_investigation = snapshot.next_investigation
        self.next_investigation_var.set(
            "Следующее: очередь пуста"
            if next_investigation is None
            else "Следующее: {id} · приоритет {priority} · {frequency} Гц · receiver {receiver}".format(
                id=next_investigation.get("id"),
                priority=next_investigation.get("priority", 0),
                frequency=next_investigation.get("target_frequency_hz"),
                receiver=next_investigation.get("target_stream_id"),
            )
        )
        self.detection_var.set(f"Обнаружение: {policy_text(snapshot.automatic_detection)}")
        self.handoff_var.set(f"Handoff: {policy_text(snapshot.automatic_handoff)}")
        spectrum_stream_id = self._spectrum_stream_id()
        needs_spectrum_refresh = (
            snapshot.session is not None
            and spectrum_stream_id != snapshot.spectrum_stream_id
        )
        self._render_spectrum()
        self._render_controls()
        if needs_details or needs_spectrum_refresh:
            self.refresh()

    def _clear_spectrum(self, message: str) -> None:
        if hasattr(self, "spectrum_view"):
            self.spectrum_view.clear()
            self._shown_view_bounds = None
            if not hasattr(self, "spectrum_canvas"):
                return
            self._sync_spectrum_controls()
        self._spectrum_draw_key = None
        self._waterfall_draw_key = None
        self.spectrum_canvas.delete("all")
        self.waterfall_canvas.delete("all")
        self._waterfall_photo = None
        self.spectrum_info_var.set(message)
        self.waterfall_info_var.set("Водопад: ожидание данных")

    def _toggle_spectrum_pause(self) -> None:
        self.spectrum_view.toggle_pause()
        self._render_spectrum()
        self._render_controls()

    def _fit_spectrum(self) -> None:
        self.spectrum_view.fit()
        self._render_spectrum()

    def _apply_spectrum_bounds(self) -> None:
        self.spectrum_view.set_bounds(self.lower_view_var.get(), self.upper_view_var.get())
        self._render_spectrum()

    def _sync_spectrum_controls(self) -> None:
        view = self.spectrum_view
        self.pause_view_button.configure(text="Live" if view.paused else "Pause view", state="normal" if view.displayed else "disabled")
        for button in (self.fit_view_button, self.apply_view_button):
            button.configure(state="normal" if view.displayed else "disabled")
        self.view_status_var.set(view.error or f"{'Unavailable' if not view.displayed else 'PAUSED view' if view.paused else 'LIVE view'} · clipped: {view.clipped} · acquisition/archive state unchanged · bounds: decimal dot, optional exponent")
        if view.bounds and view.bounds != getattr(self, "_shown_view_bounds", None):
            self._shown_view_bounds = view.bounds
            self.lower_view_var.set(str(view.bounds[0]))
            self.upper_view_var.set(str(view.bounds[1]))

    def _render_spectrum(self) -> None:
        if hasattr(self, "comparison_panel"):
            self.comparison_panel.update()
        if hasattr(self, "threshold_panel"):
            self.threshold_panel.update()
        if hasattr(self, "mask_panel"):
            self.mask_panel.update()
        if hasattr(self, "raster_panel"):
            self.raster_panel.update()
        if hasattr(self, "measurement_panel"):
            self.measurement_panel.update()
        if hasattr(self, "counter_panel"):
            self.counter_panel.update()
        if hasattr(self, "dvb_panel"):
            self.dvb_panel.update()
        if hasattr(self, "phonogram_panel"):
            self.phonogram_panel.update()
        if hasattr(self, "signal_list_panel"):
            self.signal_list_panel.update()
        if hasattr(self, "map_panel"):
            self.map_panel.update()
        if hasattr(self, "audio_stream_panel"):
            self.audio_stream_panel.update()
        if hasattr(self, "three_thresholds_panel"):
            self.three_thresholds_panel.update()
        if hasattr(self, "sweep_context_panel"):
            self.sweep_context_panel.update()
        if hasattr(self, "sweep_quality_panel"):
            self.sweep_quality_panel.update()
        if hasattr(self, "frequency_rounding_panel"):
            self.frequency_rounding_panel.update()
        if hasattr(self, "average_panel"):
            self.average_panel.update()
        stream = self._selected_stream()
        if self._spectrum_stream_id() is None:
            self._clear_spectrum(
                "Выберите поток spectrum_analyzer с исходными dBm на вкладке «Потоки». "
                "Отсчёты s_meter_count в dBm не преобразуются."
            )
            return
        if self.snapshot.spectrum_stream_id != stream.get("id"):
            self._clear_spectrum(f"Загрузка реальных sweeps потока {stream.get('id')}…")
            return
        if not self._spectrum_available:
            self._clear_spectrum("Спектр недоступен: ошибка соединения")
            return
        view = self.spectrum_view
        if not view.update(getattr(self.client, "base_url", ""), self.session_id, stream.get("id"), self.snapshot.sweeps):
            error = view.error
            self._clear_spectrum(f"Спектр недоступен: {error}")
            return
        sweeps = view.displayed
        latest = sweeps[-1]
        peak_index, peak_level, peak_frequency = peak_of(latest)
        frequencies, levels = latest["frequencies_hz"], latest["levels"]
        interval = latest["interval"]
        self._sync_spectrum_controls()
        self.spectrum_info_var.set(
            f"{'PAUSED' if view.paused else 'LIVE'} · {latest['id']} · {frequencies[0]}…{frequencies[-1]} Гц · "
            f"пик {peak_frequency} Гц, {peak_level:g} dBm · {len(levels)} точек · "
            f"UTC {interval['started_at']} … {interval['ended_at']}"
        )
        self._draw_spectrum(latest, peak_index, peak_level, peak_frequency)
        self._draw_waterfall(sweeps)

    def _draw_spectrum(
        self,
        sweep: dict[str, Any],
        peak_index: int,
        peak_level: float,
        peak_frequency: int,
    ) -> None:
        canvas = self.spectrum_canvas
        width = max(int(canvas.winfo_width()), 640)
        height = max(int(canvas.winfo_height()), 240)
        bounds = getattr(getattr(self, "spectrum_view", None), "bounds", None) or fit_bounds(sweep["levels"])
        draw_key = (repr(sweep), bounds, width, height)
        if getattr(self, "_spectrum_draw_key", None) == draw_key:
            return
        canvas.delete("all")
        left, right, top, bottom = 82, 24, 28, 52
        plot_width = max(width - left - right, 1)
        plot_height = max(height - top - bottom, 1)
        frequencies = sweep["frequencies_hz"]
        levels = sweep["levels"]
        first_frequency, last_frequency = frequencies[0], frequencies[-1]
        minimum, maximum = bounds

        def x(frequency: float) -> float:
            return left + (frequency - first_frequency) / max(
                last_frequency - first_frequency, 1
            ) * plot_width

        def y(level: float) -> float:
            return top + (1 - fraction(level, minimum, maximum)) * plot_height

        canvas.create_rectangle(0, 0, width, height, fill="#07131c", outline="")
        for row in range(5):
            py = top + plot_height * row / 4
            level = maximum * (1 - row / 4) + minimum * (row / 4)
            canvas.create_line(left, py, width - right, py, fill="#18384a")
            canvas.create_text(
                left - 10, py, text=f"{level:.1f}", fill="#9eb5c2", anchor="e"
            )
        for column in range(6):
            px = left + plot_width * column / 5
            frequency = first_frequency + (last_frequency - first_frequency) * column / 5
            canvas.create_line(px, top, px, height - bottom, fill="#18384a")
            label = (
                f"{frequency / 1_000_000:.2f} МГц"
                if abs(frequency) >= 1_000_000
                else f"{frequency:.0f} Гц"
            )
            anchor = "nw" if column == 0 else "ne" if column == 5 else "n"
            canvas.create_text(
                px, height - bottom + 12, text=label, fill="#9eb5c2", anchor=anchor
            )
        coordinates: list[float] = []
        for index in sampled_indices(len(levels), peak_index):
            coordinates.extend((x(frequencies[index]), y(levels[index])))
        if len(coordinates) >= 4:
            canvas.create_line(
                *coordinates, fill="#60e4ef", width=2, joinstyle="round"
            )
        peak_x, peak_y = x(peak_frequency), y(peak_level)
        canvas.create_line(
            peak_x, top, peak_x, height - bottom, fill="#ffd36e", dash=(5, 5)
        )
        canvas.create_oval(
            peak_x - 4, peak_y - 4, peak_x + 4, peak_y + 4,
            fill="#ffd36e", outline="",
        )
        canvas.create_text(
            left + 8, top - 10, text="ПИК · dBm", fill="#ffd36e", anchor="w"
        )
        self._spectrum_draw_key = draw_key

    def _draw_waterfall(self, sweeps: list[dict[str, Any]]) -> None:
        canvas = self.waterfall_canvas
        width = min(max(int(canvas.winfo_width()), 320), 1000)
        height = max(int(canvas.winfo_height()), 120)
        bounds = getattr(getattr(self, "spectrum_view", None), "bounds", None) or fit_bounds([v for item in sweeps for v in item["levels"]])
        draw_key = (repr(sweeps), bounds, width, height)
        if getattr(self, "_waterfall_draw_key", None) == draw_key:
            return
        canvas.delete("all")
        rows, minimum, maximum = waterfall_levels(sweeps, width)
        if not rows or minimum is None or maximum is None:
            self._waterfall_photo = None
            self.waterfall_info_var.set("Водопад: ожидание данных")
            return
        minimum, maximum = bounds
        row_height = max(1, height // len(rows))
        photo = self.tk.PhotoImage(width=width, height=row_height * len(rows))
        pixel_rows: list[str] = []
        for row in rows:
            colors = "{" + " ".join(
                waterfall_color(level, minimum, maximum) for level in row
            ) + "}"
            pixel_rows.extend([colors] * row_height)
        photo.put(" ".join(pixel_rows))
        self._waterfall_photo = photo
        self._waterfall_draw_key = draw_key
        canvas.create_image(0, 0, image=photo, anchor="nw")
        self.waterfall_info_var.set(
            f"Водопад: {len(rows)} sweeps · {minimum:g}…{maximum:g} dBm · "
            "старые ↑ / новые ↓ · sweep-order, не elapsed time · bin-order, не равные Гц · max-агрегация"
        )

    @staticmethod
    def _fill(tree: Any, records: list[dict[str, Any]], formatter: Callable[[dict[str, Any]], tuple[str, ...]]) -> None:
        selected = tree.selection()
        tree.delete(*tree.get_children())
        for index, record in enumerate(records):
            tree.insert(
                "", "end", iid=str(record.get("id", index)), values=formatter(record)
            )
        if selected and tree.exists(selected[0]):
            tree.selection_set(selected[0])

    def _select_session(self, _event: Any = None) -> None:
        index = self.session_box.current()
        if not 0 <= index < len(self.snapshot.sessions):
            return
        selected = self.snapshot.sessions[index].get("id")
        if selected == self.session_id:
            return
        self._use_created_session({"id": selected})
        if hasattr(self, "comparison_panel"):
            self.comparison_panel.update()
        if hasattr(self, "threshold_panel"):
            self.threshold_panel.update()
        if hasattr(self, "mask_panel"):
            self.mask_panel.update()
        if hasattr(self, "raster_panel"):
            self.raster_panel.update()
        if hasattr(self, "measurement_panel"):
            self.measurement_panel.update()
        if hasattr(self, "counter_panel"):
            self.counter_panel.update()
        if hasattr(self, "dvb_panel"):
            self.dvb_panel.update()
        if hasattr(self, "phonogram_panel"):
            self.phonogram_panel.update()
        if hasattr(self, "signal_list_panel"):
            self.signal_list_panel.update()
        if hasattr(self, "map_panel"):
            self.map_panel.update()
        if hasattr(self, "audio_stream_panel"):
            self.audio_stream_panel.update()
        if hasattr(self, "three_thresholds_panel"):
            self.three_thresholds_panel.update()
        if hasattr(self, "sweep_context_panel"):
            self.sweep_context_panel.update()
        if hasattr(self, "sweep_quality_panel"):
            self.sweep_quality_panel.update()
        if hasattr(self, "frequency_rounding_panel"):
            self.frequency_rounding_panel.update()
        if hasattr(self, "average_panel"):
            self.average_panel.update()
        self.refresh()

    def selected_stream_id(self) -> Optional[str]:
        selection = self.streams_tree.selection()
        return str(selection[0]) if selection else None

    def _selected_stream(self) -> Optional[dict[str, Any]]:
        if not self.session_id or (self.snapshot.session or {}).get("id") != self.session_id:
            return None
        stream_id = self.selected_stream_id()
        return next(
            (item for item in (self.snapshot.session or {}).get("streams", []) if item.get("id") == stream_id),
            None,
        )

    @staticmethod
    def _selected_record(tree: Any, records: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        selection = tree.selection()
        if not selection:
            return None
        record_id = str(selection[0])
        return next((item for item in records if str(item.get("id")) == record_id), None)

    def _selected_candidate(self) -> Optional[dict[str, Any]]:
        return self._selected_record(self.candidates_tree, self.snapshot.candidates)

    def _selected_investigation(self) -> Optional[dict[str, Any]]:
        return self._selected_record(self.investigations_tree, self.snapshot.investigations)

    def _spectrum_stream_id(self) -> Optional[str]:
        stream = self._selected_stream()
        if (
            stream
            and stream.get("role") == "spectrum_analyzer"
            and stream.get("level_unit") == "dBm"
        ):
            return str(stream.get("id"))
        return None

    def _select_stream(self, _event: Any = None) -> None:
        self._render_controls()
        stream = self._selected_stream()
        stream_id = str(stream.get("id")) if stream else None
        changed = stream_id != getattr(self, "_last_selected_stream_id", None)
        self._last_selected_stream_id = stream_id
        if hasattr(self, "comparison_panel"):
            self.comparison_panel.update()
        if hasattr(self, "threshold_panel"):
            self.threshold_panel.update()
        if hasattr(self, "mask_panel"):
            self.mask_panel.update()
        if hasattr(self, "raster_panel"):
            self.raster_panel.update()
        if hasattr(self, "measurement_panel"):
            self.measurement_panel.update()
        if hasattr(self, "counter_panel"):
            self.counter_panel.update()
        if hasattr(self, "dvb_panel"):
            self.dvb_panel.update()
        if hasattr(self, "phonogram_panel"):
            self.phonogram_panel.update()
        if hasattr(self, "signal_list_panel"):
            self.signal_list_panel.update()
        if hasattr(self, "map_panel"):
            self.map_panel.update()
        if hasattr(self, "audio_stream_panel"):
            self.audio_stream_panel.update()
        if hasattr(self, "three_thresholds_panel"):
            self.three_thresholds_panel.update()
        if hasattr(self, "sweep_context_panel"):
            self.sweep_context_panel.update()
        if hasattr(self, "sweep_quality_panel"):
            self.sweep_quality_panel.update()
        if hasattr(self, "frequency_rounding_panel"):
            self.frequency_rounding_panel.update()
        if hasattr(self, "average_panel"):
            self.average_panel.update()
        if not stream and changed:
            self._clear_spectrum("Выберите поток спектра")
        if not stream or not changed:
            return
        worker = next((item for item in self.snapshot.workers if item.get("stream_id") == stream.get("id")), {})
        current = worker.get("current") or stream.get("settings") or {}
        frequency = current.get("frequency_hz")
        if frequency:
            self.frequency_var.set(str(frequency))
        if current.get("mode"):
            self.mode_var.set(str(current["mode"]))
        if changed:
            self._render_spectrum()
            self.refresh()

    def _new_form(self, title: str) -> tuple[Any, Any, Any]:
        previous = getattr(self, "_active_form", None)
        if previous is not None:
            previous.destroy()
        window = self.tk.Toplevel(self.root)
        self._active_form = window
        window.title(title)
        window.transient(self.root)
        frame = self.ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        error_var = self.tk.StringVar()
        window.protocol("WM_DELETE_WINDOW", lambda: self._destroy_form(window))
        return window, frame, error_var

    def _destroy_form(self, window: Any) -> None:
        if getattr(self, "_active_form", None) is window:
            self._active_form = None
        window.destroy()

    def _form_entry(
        self, frame: Any, row: int, label: str, variable: Any, *, width: int = 46
    ) -> None:
        self.ttk.Label(frame, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.ttk.Entry(frame, textvariable=variable, width=width).grid(
            row=row, column=1, sticky="ew", pady=4
        )

    def _form_buttons(
        self, window: Any, frame: Any, row: int, label: str, command: Callable[[], None]
    ) -> None:
        buttons = self.ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self.ttk.Button(buttons, text=label, command=command).pack(side="left")
        self.ttk.Button(
            buttons, text="Отмена", command=lambda: self._destroy_form(window)
        ).pack(side="left", padx=(8, 0))

    def _open_session_form(self) -> None:
        if self.client is None or self._closed:
            self.action_var.set("Сначала подключитесь к общему ядру.")
            return
        window, frame, error_var = self._new_form("Новый двухприборный сеанс")
        variables = {
            "name": self.tk.StringVar(value="Windows — двойной обзор"),
            "tinysa_port": self.tk.StringVar(value="sim://tinysa"),
            "receiver_port": self.tk.StringVar(value="sim://ardv1"),
            "start_hz": self.tk.StringVar(value="88000000"),
            "stop_hz": self.tk.StringVar(value="108000000"),
            "receiver_frequency_hz": self.tk.StringVar(value="145500000"),
            "mode": self.tk.StringVar(value="FM"),
        }
        labels = (
            ("name", "Название"),
            ("tinysa_port", "Порт tinySA"),
            ("receiver_port", "Порт AR-DV1"),
            ("start_hz", "Начало обзора, Гц"),
            ("stop_hz", "Конец обзора, Гц"),
            ("receiver_frequency_hz", "Частота receiver, Гц"),
            ("mode", "Режим receiver"),
        )
        for row, (name, label) in enumerate(labels):
            self._form_entry(frame, row, label, variables[name])
        simulated_var = self.tk.BooleanVar(value=True)
        serial_var = self.tk.BooleanVar(value=False)
        self.ttk.Checkbutton(
            frame,
            text="Симуляторы tinySA и AR-DV1",
            variable=simulated_var,
            command=lambda: serial_var.set(False) if simulated_var.get() else None,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=6)
        self.ttk.Checkbutton(
            frame, text="Физические serial-приборы (FM/AM; стенд не проверен)",
            variable=serial_var,
            command=lambda: simulated_var.set(False) if serial_var.get() else None,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=6)
        self.ttk.Label(
            frame,
            text="Порты компьютера ядра: COM3/COM4 на Windows или /dev/… на Linux/macOS. "
                 "Без выбранного режима создаётся только профиль. Запуск сеанса и потоков — отдельно.",
            wraplength=520,
        ).grid(row=9, column=0, columnspan=2, sticky="ew", pady=4)
        client = self.client
        revision = self._revision

        def submit() -> None:
            if (self._closed or self._action_running or
                    getattr(self, "_active_form", None) is not window):
                return
            if self.client is not client or self._revision != revision:
                error_var.set("Контекст изменился. Откройте форму заново для текущего ядра.")
                return
            try:
                payload = session_payload(
                    **{name: variable.get() for name, variable in variables.items()},
                    simulated=bool(simulated_var.get()),
                    serial=bool(serial_var.get()),
                )
            except (TypeError, ValueError) as exc:
                error_var.set(str(exc))
                return
            self._destroy_form(window)

            def create() -> dict[str, Any]:
                result = client.create_session(payload)
                if (not isinstance(result, dict) or not isinstance(result.get("id"), str)
                        or not result["id"] or result.get("state") != "draft"):
                    raise ValueError("Некорректный ответ ядра. Обновите список: профиль мог быть создан.")
                return result

            self._submit_action(
                "Создание двухприборного сеанса",
                create,
                on_success=self._use_created_session,
            )

        self._form_buttons(window, frame, 10, "Создать сеанс", submit)
        self.ttk.Label(frame, textvariable=error_var, foreground="#b3261e", wraplength=520).grid(
            row=11, column=0, columnspan=2, sticky="ew", pady=4
        )

    def _use_created_session(self, session: dict[str, Any]) -> None:
        self._revision += 1
        self._last_selected_stream_id = None
        form = getattr(self, "_active_form", None)
        if form is not None:
            self._destroy_form(form)
        self.session_id = str(session["id"])
        self.snapshot = PlatformSnapshot(
            self.snapshot.health, self.snapshot.capabilities, self.snapshot.sessions
        )
        for tree in (self.streams_tree, self.candidates_tree, self.investigations_tree):
            tree.delete(*tree.get_children())
        self.next_investigation_var.set("Следующее: очередь загружается…")
        self._clear_spectrum("Сеанс создан; спектральные данные загружаются после выбора потока.")

    def _open_candidate_form(self, *, from_peak: bool) -> None:
        if not self._require_session():
            return
        stream = self._selected_stream()
        if not stream:
            self.action_var.set("Сначала выберите исходный поток на вкладке «Потоки».")
            return
        sweep = None
        sweep_id = None
        defaults = {
            "started_at": "",
            "ended_at": "",
            "center_frequency_hz": str(
                (stream.get("settings") or {}).get("frequency_hz", "")
            ),
            "bandwidth_hz": "",
            "peak_level": "",
            "score": "",
        }
        if from_peak:
            view = getattr(self, "spectrum_view", None)
            if view is not None and (view.paused or not view.displayed):
                self.action_var.set("Пик недоступен: вернитесь в Live с корректными данными.")
                return
            if self.snapshot.spectrum_stream_id != stream.get("id") or not self.snapshot.sweeps:
                self.action_var.set(
                    "Для фиксации пика выберите spectrum_analyzer с полученными dBm sweeps."
                )
                return
            sweep = self.snapshot.sweeps[-1]
            try:
                _index, peak_level, peak_frequency = peak_of(sweep)
            except (TypeError, ValueError) as exc:
                self.action_var.set(f"Некорректный sweep: {exc}")
                return
            frequencies = sweep["frequencies_hz"]
            step = abs(frequencies[1] - frequencies[0]) if len(frequencies) > 1 else 1
            defaults.update(
                {
                    "started_at": sweep["interval"]["started_at"],
                    "ended_at": sweep["interval"]["ended_at"],
                    "center_frequency_hz": str(peak_frequency),
                    "bandwidth_hz": str(
                        (sweep.get("settings") or {}).get("rbw_hz") or max(step, 1)
                    ),
                    "peak_level": str(peak_level),
                }
            )
            sweep_id = str(sweep["id"])
        title = "Кандидат из фактического пика" if from_peak else "Ручной кандидат"
        window, frame, error_var = self._new_form(title)
        self.ttk.Label(
            frame,
            text=f"Источник: {stream.get('id')} · единица: {stream.get('level_unit')}",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        variables = {name: self.tk.StringVar(value=value) for name, value in defaults.items()}
        labels = (
            ("started_at", "Начало интервала, UTC"),
            ("ended_at", "Конец интервала, UTC"),
            ("center_frequency_hz", "Центральная частота, Гц"),
            ("bandwidth_hz", "Оценка полосы, Гц (необязательно)"),
            ("peak_level", f"Исходный пик, {stream.get('level_unit')}"),
            ("score", "Оценка (необязательно)"),
        )
        for row, (name, label) in enumerate(labels, start=1):
            self._form_entry(frame, row, label, variables[name])
        self.ttk.Label(
            frame,
            text=(
                f"Связь с реальным sweep: {sweep_id}. Единицы не преобразуются."
                if sweep_id
                else "Интервал задаёт оператор; GUI не синтезирует измерение или его уровень."
            ),
            wraplength=560,
        ).grid(row=7, column=0, columnspan=2, sticky="ew", pady=6)
        client = self.client
        session_id = self.session_id
        source = dict(stream)

        def submit() -> None:
            view = getattr(self, "spectrum_view", None)
            if from_peak and view is not None and (view.paused or not view.displayed):
                error_var.set("Пик недоступен: вернитесь в Live с корректными данными.")
                return
            try:
                payload = candidate_payload(
                    source,
                    **{name: variable.get() for name, variable in variables.items()},
                    sweep_id=sweep_id,
                )
            except (TypeError, ValueError) as exc:
                error_var.set(str(exc))
                return
            self._destroy_form(window)
            self._submit_action(
                "Создание кандидата",
                lambda: client.create_candidate(session_id, payload),
            )

        self._form_buttons(window, frame, 8, "Создать кандидата", submit)
        self.ttk.Label(frame, textvariable=error_var, foreground="#b3261e", wraplength=560).grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=4
        )

    def _open_enqueue_form(self) -> None:
        if not self._require_session():
            return
        candidate = self._selected_candidate()
        if not candidate:
            self.action_var.set("Сначала выберите кандидата.")
            return
        receivers = [
            item
            for item in (self.snapshot.session or {}).get("streams", [])
            if item.get("role") == "receiver"
        ]
        if not receivers:
            self.action_var.set("В сеансе нет receiver для handoff.")
            return
        window, frame, error_var = self._new_form("Постановка исследования в очередь")
        self.ttk.Label(
            frame,
            text=f"Кандидат: {candidate.get('id')} · {candidate.get('center_frequency_hz')} Гц",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.ttk.Label(frame, text="Receiver").grid(row=1, column=0, sticky="w", pady=4)
        receiver_var = self.tk.StringVar(value=str(receivers[0]["id"]))
        self.ttk.Combobox(
            frame,
            textvariable=receiver_var,
            values=[str(item["id"]) for item in receivers],
            state="readonly",
        ).grid(row=1, column=1, sticky="ew", pady=4)
        current = next(
            (
                item.get("current") or {}
                for item in self.snapshot.workers
                if item.get("stream_id") == receivers[0].get("id")
            ),
            {},
        )
        settings = receivers[0].get("settings") or {}
        frequency_var = self.tk.StringVar(value=str(candidate["center_frequency_hz"]))
        mode_var = self.tk.StringVar(value=str(current.get("mode") or settings.get("mode") or ""))
        priority_var = self.tk.StringVar(value="0")
        self._form_entry(frame, 2, "Частота handoff, Гц", frequency_var)
        self._form_entry(frame, 3, "Режим", mode_var)
        self._form_entry(frame, 4, "Приоритет", priority_var)
        client = self.client
        session_id = self.session_id
        candidate_id = str(candidate["id"])

        def submit() -> None:
            try:
                payload = investigation_payload(
                    target_stream_id=receiver_var.get(),
                    target_frequency_hz=frequency_var.get(),
                    mode=mode_var.get(),
                    priority=priority_var.get(),
                )
            except (TypeError, ValueError) as exc:
                error_var.set(str(exc))
                return
            self._destroy_form(window)
            self._submit_action(
                "Постановка исследования в очередь",
                lambda: client.enqueue_investigation(session_id, candidate_id, payload),
            )

        self._form_buttons(window, frame, 5, "Поставить в очередь", submit)
        self.ttk.Label(frame, textvariable=error_var, foreground="#b3261e", wraplength=520).grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=4
        )

    def _dismiss_candidate(self) -> None:
        if not self._require_session():
            return
        candidate = self._selected_candidate()
        if not candidate:
            self.action_var.set("Сначала выберите кандидата.")
            return
        client = self.client
        session_id = self.session_id
        candidate_id = str(candidate["id"])
        self._submit_action(
            "Отклонение кандидата",
            lambda: client.dismiss_candidate(session_id, candidate_id),
        )

    def _start_next(self) -> None:
        if not self._require_session():
            return
        investigation = self.snapshot.next_investigation
        if not investigation:
            self.action_var.set("Очередь исследований пуста; сначала обновите рекомендацию.")
            return
        client = self.client
        session_id = self.session_id
        investigation_id = str(investigation["id"])
        self._submit_action(
            f"Подтверждённый запуск следующего исследования {investigation_id}",
            lambda: client.start_next_investigation(
                session_id, investigation_id, confirm=True
            ),
        )

    def _investigation_action(self, action: str) -> None:
        if not self._require_session():
            return
        investigation = self._selected_investigation()
        if not investigation:
            self.action_var.set("Сначала выберите исследование.")
            return
        client = self.client
        session_id = self.session_id
        investigation_id = str(investigation["id"])
        descriptions = {
            "start": "Ручной запуск исследования",
            "complete": "Завершение исследования",
            "fail": "Фиксация сбоя исследования",
            "cancel": "Отмена исследования",
        }
        if action == "fail":
            error = self.failure_var.get().strip()
            if not error:
                self.action_var.set("Причина сбоя не должна быть пустой.")
                return
            operation = lambda: client.fail_investigation(
                session_id, investigation_id, error=error
            )
        else:
            method = getattr(client, f"{action}_investigation")
            operation = lambda: method(session_id, investigation_id)
        self._submit_action(descriptions[action], operation)

    def _open_policy_form(self, kind: str) -> None:
        if not self._require_session():
            return
        if kind not in ("detection", "handoff"):
            raise ValueError(f"Неизвестная автоматическая политика: {kind}")
        detection = kind == "detection"
        status = (
            self.snapshot.automatic_detection
            if detection
            else self.snapshot.automatic_handoff
        ) or {}
        policy = dict(status.get("policy") or {})
        defaults = (
            {
                "interval_seconds": 1.0,
                "stream_id": None,
                "threshold_dbm": -70.0,
                "dedup_bandwidth_hz": 100000,
                "cooldown_seconds": 60.0,
                "max_candidates_per_sweep": 10,
            }
            if detection
            else {
                "interval_seconds": 1.0,
                "minimum_priority": 0,
                "receiver_stream_id": None,
                "max_consecutive_failures": 3,
                "retry_delay_seconds": 30.0,
            }
        )
        values = {**defaults, **policy}
        title = (
            "Автоматическое обнаружение кандидатов"
            if detection
            else "Автоматический handoff"
        )
        window, frame, error_var = self._new_form(title)
        fields = (
            (
                ("interval_seconds", "Период проверки, с"),
                ("threshold_dbm", "Порог, исходные dBm"),
                ("dedup_bandwidth_hz", "Полоса dedup, Гц"),
                ("cooldown_seconds", "Cooldown, с"),
                ("max_candidates_per_sweep", "Максимум кандидатов за sweep"),
            )
            if detection
            else (
                ("interval_seconds", "Период проверки, с"),
                ("minimum_priority", "Минимальный приоритет"),
                ("max_consecutive_failures", "Сбоев до отключения"),
                ("retry_delay_seconds", "Задержка повтора, с"),
            )
        )
        variables = {
            name: self.tk.StringVar(value=str(values.get(name, "")))
            for name, _label in fields
        }
        for row, (name, label) in enumerate(fields):
            self._form_entry(frame, row, label, variables[name])
        row = len(fields)
        stream_key = "stream_id" if detection else "receiver_stream_id"
        stream_var = self.tk.StringVar(value=str(values.get(stream_key) or ""))
        role = "spectrum_analyzer" if detection else "receiver"
        streams = [
            item
            for item in (self.snapshot.session or {}).get("streams", [])
            if item.get("role") == role
            and (not detection or item.get("level_unit") == "dBm")
        ]
        self.ttk.Label(
            frame,
            text="Поток (пусто — любой подходящий)",
        ).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.ttk.Combobox(
            frame,
            textvariable=stream_var,
            values=[""] + [str(item["id"]) for item in streams],
            state="readonly",
        ).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        note = (
            "Только новые dBm sweeps. Создаются кандидаты new; очередь, handoff и s_meter_count не изменяются."
            if detection
            else "Запускаются только подходящие queued-исследования. Detector и complete/fail/cancel не вызываются."
        )
        self.ttk.Label(frame, text=note, wraplength=560).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6
        )
        row += 1
        self.ttk.Label(
            frame,
            text="Runtime-only: после перезапуска сервера требуется новое явное включение.",
            wraplength=560,
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        client = self.client
        session_id = self.session_id
        method = (
            client.configure_automatic_detection
            if detection
            else client.configure_automatic_handoff
        )

        def make_payload(enabled: bool) -> dict[str, Any]:
            parameters = {
                name: variable.get() for name, variable in variables.items()
            }
            parameters[stream_key] = stream_var.get()
            builder = (
                automatic_detection_payload
                if detection
                else automatic_handoff_payload
            )
            return builder(enabled=enabled, **parameters)

        def submit(enabled: bool) -> None:
            if enabled and (self.snapshot.session or {}).get("state") != "running":
                error_var.set(
                    "Автоматическую политику можно включить только в работающем сеансе."
                )
                return
            try:
                payload = make_payload(enabled)
            except (TypeError, ValueError) as exc:
                error_var.set(str(exc))
                return
            self._destroy_form(window)
            name = "обнаружения" if detection else "handoff"
            description = (
                f"Явное включение {name}"
                if enabled
                else f"Сохранение выключенной политики {name}"
            )
            self._submit_action(
                description,
                lambda: method(session_id, payload),
            )

        row += 1
        buttons = self.ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        enabled = bool(policy.get("enabled"))
        if enabled:
            self.ttk.Button(
                buttons,
                text="Сохранить включённой",
                command=lambda: submit(True),
            ).pack(side="left")
            self.ttk.Button(
                buttons,
                text="Выключить",
                command=lambda: (
                    self._destroy_form(window),
                    self._disable_policy(kind, session_id=session_id, client=client),
                ),
            ).pack(side="left", padx=(8, 0))
        else:
            self.ttk.Button(
                buttons,
                text="Сохранить выключенной",
                command=lambda: submit(False),
            ).pack(side="left")
            self.ttk.Button(
                buttons,
                text="Включить явно",
                command=lambda: submit(True),
            ).pack(side="left", padx=(8, 0))
        self.ttk.Button(
            buttons,
            text="Отмена",
            command=lambda: self._destroy_form(window),
        ).pack(side="left", padx=(8, 0))
        row += 1
        self.ttk.Label(
            frame, textvariable=error_var, foreground="#b3261e", wraplength=560
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)

    def _disable_policy(self, kind: str, *, session_id=None, client=None) -> None:
        if ((session_id is not None and session_id != self.session_id)
                or (client is not None and client is not self.client)):
            return
        if not self._require_session():
            return
        if kind not in ("detection", "handoff"):
            raise ValueError(f"Неизвестная автоматическая политика: {kind}")
        client = self.client
        session_id = self.session_id
        method = (
            client.configure_automatic_detection
            if kind == "detection"
            else client.configure_automatic_handoff
        )
        name = "обнаружения" if kind == "detection" else "handoff"
        self._submit_action(
            f"Выключение {name}; другая политика не изменяется",
            lambda: method(session_id, {"enabled": False}),
        )

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
        stream = self._selected_stream()
        stream_id = stream.get("id") if stream else None
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

        def set_state(name: str, enabled: bool) -> None:
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state="normal" if enabled else "disabled")

        for name in ("session_start", "session_stop", "workers_start", "workers_stop"):
            set_state(name, selected)
        set_state("session_create", connected)
        set_state("detection_policy_button", selected)
        set_state("handoff_policy_button", selected)
        stream_selected = selected and self.selected_stream_id() is not None
        for name in ("stream_start", "stream_stop"):
            set_state(name, stream_selected)
        stream = self._selected_stream() if stream_selected else None
        set_state("tune_button", bool(stream and stream.get("role") == "receiver"))
        set_state("manual_candidate_button", bool(stream))
        peak_available = bool(
            stream
            and self.snapshot.spectrum_stream_id == stream.get("id")
            and self.snapshot.sweeps
        )
        view = getattr(self, "spectrum_view", None)
        peak_available = peak_available and (view is None or bool(view.displayed) and not view.paused)
        set_state("peak_candidate_button", peak_available)
        candidate = (
            self._selected_candidate()
            if selected and hasattr(self, "candidates_tree")
            else None
        )
        candidate_editable = bool(
            candidate and candidate.get("state") in ("new", "queued")
        )
        set_state("enqueue_button", candidate_editable)
        set_state("dismiss_button", candidate_editable)
        investigation = (
            self._selected_investigation()
            if selected and hasattr(self, "investigations_tree")
            else None
        )
        state = investigation.get("state") if investigation else None
        running = (self.snapshot.session or {}).get("state") == "running"
        set_state("investigation_start", selected and running and state == "queued")
        set_state("investigation_complete", state in ("listening", "decoding"))
        set_state("investigation_fail", state in ("queued", "tuning", "listening", "decoding"))
        set_state("investigation_cancel", state in ("queued", "tuning", "listening", "decoding"))
        set_state(
            "start_next_button",
            selected and running and self.snapshot.next_investigation is not None,
        )
        set_state("refresh_button", self.client is not None)

    def close(self) -> None:
        if self._closed:
            return
        self._shutdown()
        self.root.destroy()

    def _shutdown(self) -> None:
        """Release resources even when root.destroy already removed children."""
        with self._message_lock:
            if self._closed:
                return
            self._closed = True
            self._messages_closed.set()
        # Future.cancel/shutdown can call _queue_message synchronously. Never
        # hold the publication lock while stopping an executor or panel.
        self._close_timers()
        if hasattr(self, "monitoring_panel"):
            self.monitoring_panel.close()
        if hasattr(self, "comparison_panel"):
            self.comparison_panel.close()
        if hasattr(self, "threshold_panel"):
            self.threshold_panel.close()
        if hasattr(self, "mask_panel"):
            self.mask_panel.close()
        if hasattr(self, "raster_panel"):
            self.raster_panel.close()
        if hasattr(self, "measurement_panel"):
            self.measurement_panel.close()
        if hasattr(self, "counter_panel"):
            self.counter_panel.close()
        if hasattr(self, "dvb_panel"):
            self.dvb_panel.close()
        if hasattr(self, "phonogram_panel"):
            self.phonogram_panel.close()
        if hasattr(self, "signal_list_panel"):
            self.signal_list_panel.close()
        if hasattr(self, "map_panel"):
            self.map_panel.close()
        if hasattr(self, "audio_stream_panel"):
            self.audio_stream_panel.close()
        if hasattr(self, "tinysa_panel"):
            self.tinysa_panel.close()
        if hasattr(self, "average_panel"):
            self.average_panel.close()
        if hasattr(self, "three_thresholds_panel"):
            self.three_thresholds_panel.close()
        if hasattr(self, "sweep_context_panel"):
            self.sweep_context_panel.close()
        if hasattr(self, "sweep_quality_panel"):
            self.sweep_quality_panel.close()
        if hasattr(self, "frequency_rounding_panel"):
            self.frequency_rounding_panel.close()
        self._action_callbacks.clear()
        getattr(self, '_project_action_clients', {}).clear()
        self._refresh_running = self._action_running = False
        self._executor.shutdown(wait=False, cancel_futures=True)
        while True:
            try:
                self._messages.get_nowait()
            except queue.Empty:
                break


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
