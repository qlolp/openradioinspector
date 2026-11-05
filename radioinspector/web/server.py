"""Веб-интерфейс OpenRadioInspector.

Реализован на стандартной библиотеке: HTTP-сервер на базе
``http.server`` и потоковая выдача обновлений по протоколу Server-Sent
Events. Внешние зависимости не требуются, что позволяет запускать интерфейс
на том же компьютере, где работает измерительный комплекс.

Интерфейс обеспечивает:

* отображение текущей панорамы и накопленных графиков максимумов, минимумов
  и усреднённых значений;
* спектрограмму («водопад») в плоском и объёмном представлениях;
* редактирование линии порога и просмотр списка сигналов, превысивших порог;
* измерение параметров сигнала по выбранному поддиапазону;
* отображение точек измерений на карте и карту распределения напряжённости
  поля.
"""

from __future__ import annotations

import json
import mimetypes
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import numpy as np
from urllib.parse import parse_qs, unquote, urlparse

from ..assignments import AssignmentDatabase
from ..automatic_detection import AutomaticCandidateDetector
from ..automatic_handoff import AutomaticHandoffScheduler
from ..contracts import api_payload
from ..device_workers import DeviceWorkerManager, UnsupportedAdapterError, WorkerError
from ..session_service import (
    CandidateNotFoundError,
    InvestigationNotFoundError,
    SessionNotFoundError,
    SessionService,
)
from ..session_store import SQLiteSessionStore
from ..jsonio import dumps as json_dumps
from ..keywords import CATALOG
from ..geoloc import MeasurementPoint, build_coverage_map
from ..measurements import measure_signal
from ..occupancy import efficiency_from_store, occupancy_from_store
from ..panorama import PanoramaLibrary, PanoramaStore
from ..signals import SignalList
from ..threshold import ThresholdLine

STATIC_DIR = Path(__file__).parent / "static"


class ApplicationState:
    """Состояние веб-приложения: текущая база данных и список сигналов."""

    def __init__(self, database: Optional[str] = None, library: Optional[str] = None) -> None:
        self.database = database
        self.library = library
        self.store: Optional[PanoramaStore] = None
        self.signals = SignalList()
        self.threshold: Optional[ThresholdLine] = None
        self._subscribers: list[threading.Event] = []
        self._lock = threading.Lock()
        self._version = 0

        if database:
            self.load_database(database)

    # -- работа с базой данных ---------------------------------------------

    def load_database(self, path: str) -> None:
        """Открывает базу данных панорам и перечитывает порог и сигналы."""
        store = PanoramaStore(path)
        self.store = store
        self.database = path

        threshold = store.load_threshold(0)
        if threshold is None:
            latest = store.latest()
            threshold = (
                ThresholdLine.default_for(latest.trace)
                if latest is not None
                else ThresholdLine.horizontal(0.0, 1.0, -80.0)
            )
        self.threshold = threshold

        signals_file = Path(path).with_name("signals.json")
        if signals_file.exists():
            self.signals = SignalList.load(signals_file)
        else:
            self.signals = SignalList()
            for record in store.iter_panoramas():
                self.signals.update_from_trace(record.trace, threshold, record.recorded_at)

        self.bump()

    def reload_signals(self) -> None:
        """Пересчёт списка сигналов по текущей линии порога."""
        if self.store is None or self.threshold is None:
            return
        self.signals = SignalList()
        for record in self.store.iter_panoramas():
            self.signals.update_from_trace(record.trace, self.threshold, record.recorded_at)
        self.bump()

    # -- подписки -----------------------------------------------------------

    def subscribe(self) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._subscribers.append(event)
        return event

    def unsubscribe(self, event: threading.Event) -> None:
        with self._lock:
            if event in self._subscribers:
                self._subscribers.remove(event)

    def bump(self) -> None:
        """Сигнализирует подписчикам об изменении состояния."""
        with self._lock:
            self._version += 1
            subscribers = list(self._subscribers)
        for event in subscribers:
            event.set()

    @property
    def version(self) -> int:
        return self._version

    # -- снимок состояния ---------------------------------------------------

    def snapshot(self, include_waterfall: bool = True) -> dict:
        payload: dict = {
            "version": self._version,
            "database": self.database,
            "signals": self.signals.to_rows(),
            "threshold": self.threshold.as_dict() if self.threshold else None,
        }
        if self.store is None:
            payload.update(
                {
                    "panoramas": 0,
                    "current": None,
                    "max": None,
                    "min": None,
                    "average": None,
                    "waterfall": [],
                    "points": [],
                    "settings": None,
                }
            )
            return payload

        latest = self.store.latest()
        settings = self.store.read_settings()
        payload["panoramas"] = self.store.count
        payload["settings"] = (
            {
                "device": settings.device,
                "antenna": settings.antenna,
                "tract": settings.tract,
                "start_hz": settings.start_hz,
                "stop_hz": settings.stop_hz,
                "bandwidth_hz": settings.bandwidth_hz,
                "step_hz": settings.step_hz,
                "points": settings.points,
            }
            if settings
            else None
        )
        payload["current"] = latest.trace.to_dict() if latest else None
        max_trace = self.store.max_trace()
        min_trace = self.store.min_trace()
        avg_trace = self.store.average_trace()
        payload["max"] = max_trace.to_dict() if max_trace else None
        payload["min"] = min_trace.to_dict() if min_trace else None
        payload["average"] = avg_trace.to_dict() if avg_trace else None

        if include_waterfall:
            grid, times = self.store.level_grid()
            limit = 300
            if grid.shape[0] > limit:
                grid = grid[-limit:]
                times = times[-limit:]
            payload["waterfall"] = {
                "levels": grid.tolist(),
                "times": times.tolist(),
                "frequencies": self.store.frequencies().tolist(),
            }

        points = []
        for record in self.store.iter_panoramas():
            if record.latitude is None or record.longitude is None:
                continue
            points.append(
                {
                    "latitude": record.latitude,
                    "longitude": record.longitude,
                    "level_db": record.trace.peak,
                    "recorded_at": record.recorded_at.isoformat(),
                }
            )
        payload["points"] = points
        return payload

    def measurements(self, start_hz: float, stop_hz: float, norm_hz: Optional[float] = None) -> dict:
        if self.store is None:
            return {}
        latest = self.store.latest()
        if latest is None:
            return {}
        trace = latest.trace.slice(start_hz, stop_hz)
        return measure_signal(trace, norm_hz=norm_hz)

    def occupancy(self, threshold_db: Optional[float] = None) -> dict:
        """Оценка загрузки диапазона по сохранённой базе данных панорам."""
        if self.store is None:
            return {}
        if threshold_db is None:
            threshold_db = self.threshold_level()
        return occupancy_from_store(self.store, threshold_db).as_dict()

    def efficiency(self, threshold_db: Optional[float] = None) -> dict:
        """Эффективность использования диапазона: доля моментов наблюдения."""
        if self.store is None:
            return {}
        if threshold_db is None:
            threshold_db = self.threshold_level()
        return efficiency_from_store(self.store, threshold_db).as_dict()

    def threshold_level(self) -> float:
        """Уровень линии порога, дБ; при её отсутствии — 2/3 шкалы."""
        if self.threshold is not None and self.threshold.levels.size:
            return float(np.median(self.threshold.levels))
        latest = self.store.latest() if self.store is not None else None
        if latest is None or not latest.trace.size:
            return -100.0
        return float(latest.trace.peak - 30.0)

    def coverage(self, rows: int = 30, cols: int = 30) -> dict:
        if self.store is None:
            return {}
        points = [
            MeasurementPoint(
                latitude=rec.latitude,
                longitude=rec.longitude,
                level_db=rec.trace.peak,
            )
            for rec in self.store.iter_panoramas()
            if rec.latitude is not None and rec.longitude is not None
        ]
        if not points:
            return {}
        return build_coverage_map(points, rows=rows, cols=cols).to_geojson()


class Handler(BaseHTTPRequestHandler):
    """Обработчик HTTP-запросов веб-интерфейса."""

    server_version = "OpenRadioInspector"
    state: ApplicationState
    sessions: SessionService
    workers: DeviceWorkerManager

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - сигнатура базового класса
        return

    # -- вспомогательные методы --------------------------------------------

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json_dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Обновление страницы может отменить ожидающий ответ без сбоя API.
            pass

    def _send_api_error(self, status: int, code: str, message: str) -> None:
        self._send_json(
            {
                "contract": "openradioinspector-platform",
                "contract_version": 1,
                "error": {"code": code, "message": message},
            },
            status,
        )

    def _send_file(self, path: Path) -> None:
        if not path.exists():
            self.send_error(404, "Not found")
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # -- маршруты ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - имя задано базовым классом
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route.startswith("/api/v1/"):
            self._api_v1_get(route, query)
            return

        if route in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html")
            return
        if route in ("/platform", "/platform.html"):
            self._send_file(STATIC_DIR / "platform.html")
            return
        if route == "/api/state":
            self._send_json(self.state.snapshot())
            return
        if route == "/api/signals":
            self._send_json({"signals": self.state.signals.to_rows()})
            return
        if route == "/api/coverage":
            rows = int(query.get("rows", ["30"])[0])
            cols = int(query.get("cols", ["30"])[0])
            self._send_json(self.state.coverage(rows, cols))
            return
        if route == "/api/occupancy":
            raw = query.get("threshold", [None])[0]
            threshold = float(raw) if raw not in (None, "") else None
            self._send_json(self.state.occupancy(threshold))
            return
        if route == "/api/efficiency":
            raw = query.get("threshold", [None])[0]
            threshold = float(raw) if raw not in (None, "") else None
            self._send_json(self.state.efficiency(threshold))
            return
        if route == "/api/keywords":
            self._send_json({"keywords": CATALOG, "count": len(CATALOG)})
            return
        if route == "/api/measure":
            start = float(query.get("start", ["0"])[0])
            stop = float(query.get("stop", ["0"])[0])
            raw_norm = query.get("norm", [None])[0]
            norm = float(raw_norm) if raw_norm not in (None, "") else None
            self._send_json(self.state.measurements(start, stop, norm))
            return
        if route == "/api/stream":
            self._stream_events()
            return
        if route.startswith("/static/"):
            name = route[len("/static/") :]
            target = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() in target.parents:
                self._send_file(target)
            else:
                self.send_error(403, "Forbidden")
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802 - имя задано базовым классом
        parsed = urlparse(self.path)
        route = parsed.path
        payload = self._read_json()

        if route.startswith("/api/v1/"):
            self._api_v1_post(route, payload)
            return

        if route == "/api/threshold":
            line = self.state.threshold
            if line is None:
                self._send_json({"ok": False, "error": "нет активной линии порога"}, 400)
                return
            if "offset_db" in payload:
                line = line.shifted(float(payload["offset_db"]))
            if "levels" in payload and "frequencies" in payload:
                import numpy as np

                line = ThresholdLine(
                    np.asarray(payload["frequencies"], dtype=float),
                    np.asarray(payload["levels"], dtype=float),
                    line.index,
                    line.unit,
                )
            self.state.threshold = line
            if self.state.store is not None:
                self.state.store.save_threshold(line)
            self.state.reload_signals()
            self._send_json({"ok": True, "threshold": line.as_dict()})
            return

        if route == "/api/database":
            path = payload.get("path")
            if not path:
                self._send_json({"ok": False, "error": "не указан путь"}, 400)
                return
            try:
                self.state.load_database(path)
            except Exception as exc:  # noqa: BLE001 - сообщаем ошибку клиенту
                self._send_json({"ok": False, "error": str(exc)}, 400)
                return
            self._send_json({"ok": True, "version": self.state.version})
            return

        self.send_error(404, "Not found")

    # -- общий API платформы, версия 1 --------------------------------------

    def _api_v1_get(self, route: str, query: dict) -> None:
        try:
            if route == "/api/v1/health":
                self._send_json(api_payload(self.sessions.health()))
                return
            if route == "/api/v1/capabilities":
                capabilities = self.sessions.capabilities()
                capabilities["features"].update(self.workers.capabilities())
                self._send_json(api_payload(capabilities))
                return
            if route == "/api/v1/sessions":
                self._send_json(api_payload(self.sessions.list_sessions()))
                return
            if route == "/api/v1/workers":
                self._send_json(api_payload(self.workers.list_workers()))
                return
            if route == "/api/v1/events/history":
                after = int(query.get("after", ["0"])[0])
                limit = min(int(query.get("limit", ["100"])[0]), 1000)
                self._send_json(
                    api_payload(self.sessions.events(after=after, limit=limit))
                )
                return
            if route == "/api/v1/events":
                raw_after = self.headers.get("Last-Event-ID") or query.get(
                    "after", ["0"]
                )[0]
                self._stream_session_events(int(raw_after))
                return
            parts = route.strip("/").split("/")
            if len(parts) >= 4 and parts[:3] == ["api", "v1", "sessions"]:
                # Декодируем только ID после разделения: encoded slash остаётся внутри ID.
                parts[3] = unquote(parts[3])
            if (
                len(parts) >= 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "streams"
            ):
                parts[5] = unquote(parts[5])
            if (
                len(parts) >= 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] in ("candidates", "investigations")
            ):
                parts[5] = unquote(parts[5])
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] in ("candidates", "investigations")
            ):
                state = query.get("state", [None])[0]
                if parts[4] == "candidates":
                    result = self.sessions.list_candidates(parts[3], state=state)
                else:
                    result = self.sessions.list_investigations(parts[3], state=state)
                self._send_json(api_payload(result))
                return
            if (
                len(parts) == 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "investigations"
                and parts[5] == "next"
            ):
                self._send_json(api_payload(self.sessions.next_investigation(parts[3])))
                return
            if (
                len(parts) == 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] in ("candidates", "investigations")
            ):
                if parts[4] == "candidates":
                    result = self.sessions.get_candidate(parts[3], parts[5])
                else:
                    result = self.sessions.get_investigation(parts[3], parts[5])
                self._send_json(api_payload(result))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "automatic-handoff"
            ):
                self._send_json(api_payload(self.automatic_handoff.status(parts[3])))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "automatic-detection"
            ):
                self._send_json(api_payload(self.automatic_detection.status(parts[3])))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "workers"
            ):
                self._send_json(api_payload(self.workers.list_workers(parts[3])))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] in ("sweeps", "observations")
            ):
                stream_id = query.get("stream_id", [None])[0]
                limit = int(query.get("limit", ["100"])[0])
                if parts[4] == "sweeps":
                    measurements = self.sessions.list_sweeps(
                        parts[3], stream_id=stream_id, limit=limit
                    )
                else:
                    measurements = self.sessions.list_observations(
                        parts[3], stream_id=stream_id, limit=limit
                    )
                self._send_json(api_payload(measurements))
                return
            if len(parts) == 4 and parts[:3] == ["api", "v1", "sessions"]:
                self._send_json(api_payload(self.sessions.get_session(parts[3])))
                return
            self._send_api_error(404, "not_found", "Маршрут API v1 не найден")
        except SessionNotFoundError as exc:
            self._send_api_error(404, "session_not_found", str(exc.args[0]))
        except CandidateNotFoundError as exc:
            self._send_api_error(404, "candidate_not_found", str(exc.args[0]))
        except InvestigationNotFoundError as exc:
            self._send_api_error(404, "investigation_not_found", str(exc.args[0]))
        except (TypeError, ValueError) as exc:
            self._send_api_error(400, "invalid_request", str(exc))

    def _api_v1_post(self, route: str, payload: dict) -> None:
        try:
            if route == "/api/v1/sessions":
                session = self.sessions.create_session(payload)
                self._send_json(api_payload(session), 201)
                return
            parts = route.strip("/").split("/")
            if len(parts) >= 4 and parts[:3] == ["api", "v1", "sessions"]:
                # Декодируем только ID после разделения: encoded slash остаётся внутри ID.
                parts[3] = unquote(parts[3])
            if (
                len(parts) >= 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "streams"
            ):
                parts[5] = unquote(parts[5])
            if (
                len(parts) >= 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] in ("candidates", "investigations")
            ):
                parts[5] = unquote(parts[5])
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "automatic-handoff"
            ):
                result = self.automatic_handoff.configure(parts[3], payload)
                self._send_json(api_payload(result))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "automatic-detection"
            ):
                result = self.automatic_detection.configure(parts[3], payload)
                self._send_json(api_payload(result))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "candidates"
            ):
                candidate = self.sessions.create_candidate(parts[3], payload)
                self._send_json(api_payload(candidate), 201)
                return
            if (
                len(parts) == 7
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "candidates"
                and parts[6] in ("investigations", "dismiss")
            ):
                if parts[6] == "investigations":
                    result = self.sessions.enqueue_investigation(
                        parts[3], parts[5], payload
                    )
                    self._send_json(api_payload(result), 201)
                else:
                    result = self.sessions.dismiss_candidate(parts[3], parts[5])
                    self._send_json(api_payload(result))
                return
            if (
                len(parts) == 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "investigations"
                and parts[5] == "start-next"
            ):
                if payload.get("confirm") is not True:
                    raise ValueError(
                        "Полуавтоматический handoff требует явного confirm=true"
                    )
                result = self.sessions.start_next_investigation(
                    parts[3], payload["investigation_id"], tune=self.workers.tune
                )
                self._send_json(api_payload(result))
                return
            if (
                len(parts) == 7
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "investigations"
                and parts[6] in ("start", "complete", "fail", "cancel")
            ):
                if parts[6] == "start":
                    result = self.sessions.start_investigation(
                        parts[3], parts[5], tune=self.workers.tune
                    )
                elif parts[6] == "complete":
                    result = self.sessions.complete_investigation(parts[3], parts[5])
                elif parts[6] == "fail":
                    result = self.sessions.fail_investigation(
                        parts[3], parts[5], payload.get("error")
                    )
                else:
                    result = self.sessions.cancel_investigation(parts[3], parts[5])
                self._send_json(api_payload(result))
                return
            if (
                len(parts) == 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "workers"
                and parts[5] in ("start", "stop")
            ):
                if parts[5] == "start":
                    result = self.workers.start_session(parts[3])
                else:
                    result = self.workers.stop_session(parts[3])
                self._send_json(api_payload(result))
                return
            if (
                len(parts) == 7
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "streams"
                and parts[6] in ("start", "stop", "tune")
            ):
                if parts[6] == "start":
                    result = self.workers.start_stream(parts[3], parts[5])
                elif parts[6] == "stop":
                    result = self.workers.stop_stream(parts[3], parts[5])
                else:
                    result = self.workers.tune(parts[3], parts[5], payload)
                self._send_json(api_payload(result))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] in ("sweeps", "observations")
            ):
                measurement = dict(payload)
                supplied_session = measurement.get("session_id")
                if supplied_session is not None and supplied_session != parts[3]:
                    raise ValueError("session_id в теле не совпадает с маршрутом")
                measurement["session_id"] = parts[3]
                if parts[4] == "sweeps":
                    result = self.sessions.ingest_sweep(measurement)
                else:
                    result = self.sessions.ingest_observation(measurement)
                self._send_json(api_payload(result), 201)
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] in ("start", "stop")
            ):
                if parts[4] == "start":
                    session = self.sessions.start_session(parts[3])
                else:
                    self.workers.stop_session(parts[3])
                    session = self.sessions.stop_session(parts[3])
                self._send_json(api_payload(session))
                return
            self._send_api_error(404, "not_found", "Маршрут API v1 не найден")
        except SessionNotFoundError as exc:
            self._send_api_error(404, "session_not_found", str(exc.args[0]))
        except CandidateNotFoundError as exc:
            self._send_api_error(404, "candidate_not_found", str(exc.args[0]))
        except InvestigationNotFoundError as exc:
            self._send_api_error(404, "investigation_not_found", str(exc.args[0]))
        except KeyError as exc:
            self._send_api_error(400, "missing_field", f"Отсутствует поле: {exc.args[0]}")
        except UnsupportedAdapterError as exc:
            self._send_api_error(400, "unsupported_adapter", str(exc))
        except WorkerError as exc:
            self._send_api_error(400, "worker_error", str(exc))
        except (TypeError, ValueError) as exc:
            self._send_api_error(400, "invalid_request", str(exc))

    def _stream_session_events(self, after: int = 0) -> None:
        """SSE с монотонным id и повторной выдачей после Last-Event-ID."""
        if after < 0:
            raise ValueError("Last-Event-ID должен быть неотрицательным")
        subscriber_id, events = self.sessions.subscribe(after=after)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                try:
                    event = events.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                data = json_dumps(api_payload(event))
                frame = (
                    f"id: {event.sequence}\n"
                    f"event: {event.event_type}\n"
                    f"data: {data}\n\n"
                )
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.sessions.unsubscribe(subscriber_id)

    def _stream_events(self) -> None:
        """Потоковая выдача состояния по протоколу Server-Sent Events."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        event = self.state.subscribe()
        last_version = -1
        try:
            while True:
                if event.wait(timeout=1.0):
                    event.clear()
                if self.state.version != last_version:
                    last_version = self.state.version
                    data = json_dumps(self.state.snapshot(include_waterfall=False))
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.state.unsubscribe(event)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8770,
    database: Optional[str] = None,
    library: Optional[str] = None,
    sessions: Optional[SessionService] = None,
    session_database: Optional[str] = None,
    workers: Optional[DeviceWorkerManager] = None,
    automatic_handoff: Optional[AutomaticHandoffScheduler] = None,
    automatic_detection: Optional[AutomaticCandidateDetector] = None,
) -> ThreadingHTTPServer:
    """Создаёт HTTP-сервер; отдельная функция упрощает встраивание и тесты."""
    if sessions is not None and session_database is not None:
        raise ValueError("Передайте либо sessions, либо session_database, но не оба")
    state = ApplicationState(database=database, library=library)
    if sessions is not None:
        session_service = sessions
    elif session_database:
        session_service = SessionService(store=SQLiteSessionStore(session_database))
    else:
        session_service = SessionService()
    if workers is not None and workers.sessions is not session_service:
        raise ValueError("DeviceWorkerManager должен использовать тот же SessionService")
    worker_manager = workers or DeviceWorkerManager(session_service)
    if automatic_handoff is not None and automatic_handoff.sessions is not session_service:
        raise ValueError("AutomaticHandoffScheduler должен использовать тот же SessionService")
    automatic_scheduler = automatic_handoff or AutomaticHandoffScheduler(
        session_service, tune=worker_manager.tune
    )
    if automatic_detection is not None and automatic_detection.sessions is not session_service:
        raise ValueError("AutomaticCandidateDetector должен использовать тот же SessionService")
    detection_scheduler = automatic_detection or AutomaticCandidateDetector(session_service)
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "state": state,
            "sessions": session_service,
            "workers": worker_manager,
            "automatic_handoff": automatic_scheduler,
            "automatic_detection": detection_scheduler,
        },
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.session_service = session_service
    httpd.worker_manager = worker_manager
    httpd.automatic_handoff = automatic_scheduler
    httpd.automatic_detection = detection_scheduler
    return httpd


def serve(
    host: str = "127.0.0.1",
    port: int = 8770,
    database: Optional[str] = None,
    library: Optional[str] = None,
    session_database: Optional[str] = None,
) -> None:
    """Запуск веб-интерфейса."""
    httpd = create_server(
        host=host,
        port=port,
        database=database,
        library=library,
        session_database=session_database,
    )
    print(f"Веб-интерфейс: http://{host}:{port}")
    if database:
        print(f"База данных панорам: {database}")
    if session_database:
        print(f"Архив сеансов: {session_database}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")
    finally:
        httpd.automatic_detection.close()
        httpd.automatic_handoff.close()
        httpd.worker_manager.close()
        httpd.server_close()
        httpd.session_service.close()
