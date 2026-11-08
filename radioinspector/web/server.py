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
import math
import mimetypes
import queue
import re
import tempfile
import threading
import time
from contextlib import ExitStack
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path
from typing import Optional

import numpy as np
from urllib.parse import parse_qs, unquote, urlparse

from ..archive_export import build_archive_document
from ..archive_export_stream import iter_snapshot_ndjson, open_archive_snapshot
from ..assignments import AssignmentDatabase
from ..automatic_completion import AutomaticInvestigationCompletion
from ..automatic_detection import AutomaticCandidateDetector
from ..automatic_handoff import AutomaticHandoffScheduler
from ..contracts import api_payload
from ..frequency_rounding import MAX_ROUNDING_REQUEST_BYTES, FrequencyRoundingIncompatibleError
from ..device_workers import DeviceWorkerManager, UnsupportedAdapterError, WorkerError
from ..session_service import (
    CandidateNotFoundError,
    InvestigationNotFoundError,
    SessionNotFoundError,
    SessionService,
    SweepNotFoundError,
)
from ..session_analytics import analytics_summary
from ..session_store import ArchiveCapacityExceededError, SQLiteSessionStore
from ..sweep_average import MAX_AVERAGE_REQUEST_BYTES, AverageReferenceIncompatibleError
from ..spectral_mask import MAX_MASK_REQUEST_BYTES, MaskIncompatibleError, strict_json_loads
from ..sweep_comparison import ComparisonIncompatibleError
from ..sweep_context import MAX_CONTEXT_REQUEST_BYTES, ContextIncompatibleError
from ..sweep_threshold import ThresholdIncompatibleError
from ..sweep_three_thresholds import MAX_THREE_THRESHOLDS_REQUEST_BYTES, ThreeThresholdsIncompatibleError
from ..sweep_references import ReferenceLimitReachedError, ReferenceNameConflictError
from ..task_scheduler import TaskNotFoundError, TaskScheduler
from ..tasks import TaskBook
from ..jsonio import dumps as json_dumps
from ..keywords import CATALOG
from ..geoloc import MeasurementPoint, build_coverage_map
from ..measurements import measure_signal
from ..occupancy import efficiency_from_store, occupancy_from_store
from ..panorama import PanoramaLibrary, PanoramaStore
from ..signals import SignalList
from ..threshold import ThresholdLine

STATIC_DIR = Path(__file__).parent / "static"


def state_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class ThreadingHTTPServer(_ThreadingHTTPServer):
    """Core startup must not depend on reverse DNS for a bound interface."""
    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


class ApplicationState:
    """Состояние веб-приложения: текущая база данных и список сигналов."""

    def __init__(self, database: Optional[str] = None, library: Optional[str] = None) -> None:
        self.database = database
        self.library = library
        self.store: Optional[PanoramaStore] = None
        self.signals = SignalList()
        self.threshold: Optional[ThresholdLine] = None
        self._subscribers: list[threading.Event] = []
        self._lock = threading.RLock()
        self._version = 0

        if database:
            self.load_database(database)

    def close(self) -> None:
        with self._lock:
            if self.store is not None:
                self.store.close()
                self.store = None

    # -- работа с базой данных ---------------------------------------------

    def load_database(self, path: str) -> None:
        """Открывает базу данных панорам и перечитывает порог и сигналы."""
        candidate = Path(path).resolve()
        if not candidate.is_file() or candidate.suffix.lower() != ".pan":
            raise ValueError("Укажите существующую базу панорам .pan")
        if self.library is not None and not candidate.is_relative_to(Path(self.library).resolve()):
            raise ValueError("База находится вне настроенного каталога")
        store = PanoramaStore(candidate)
        try:
            threshold = store.load_threshold(0)
            if threshold is None:
                latest = store.latest()
                threshold = (ThresholdLine.default_for(latest.trace) if latest is not None
                             else ThresholdLine.horizontal(0.0, 1.0, -80.0))
            signals_file = candidate.with_name("signals.json")
            if signals_file.exists():
                signals = SignalList.load(signals_file)
            else:
                signals = SignalList()
                for record in store.iter_panoramas():
                    signals.update_from_trace(record.trace, threshold, record.recorded_at)
        except Exception:
            store.close()
            raise
        # Readers hold the same lock: never close a connection still in use.
        with self._lock:
            old = self.store
            self.store, self.database = store, str(candidate)
            self.signals, self.threshold = signals, threshold
            self.bump()
            if old is not None:
                old.close()

    @state_locked
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
        """Сигнализирует подписчиков об изменении состояния."""
        with self._lock:
            self._version += 1
            subscribers = list(self._subscribers)
        for event in subscribers:
            event.set()

    @property
    def version(self) -> int:
        return self._version

    # -- снимок состояния ---------------------------------------------------

    @state_locked
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

    @state_locked
    def measurements(self, start_hz: float, stop_hz: float, norm_hz: Optional[float] = None) -> dict:
        if self.store is None:
            return {}
        latest = self.store.latest()
        if latest is None:
            return {}
        trace = latest.trace.slice(start_hz, stop_hz)
        return measure_signal(trace, norm_hz=norm_hz)

    @state_locked
    def occupancy(self, threshold_db: Optional[float] = None) -> dict:
        """Оценка загрузки диапазона по сохранённой базе данных панорам."""
        if self.store is None:
            return {}
        if threshold_db is None:
            threshold_db = self.threshold_level()
        return occupancy_from_store(self.store, threshold_db).as_dict()

    @state_locked
    def efficiency(self, threshold_db: Optional[float] = None) -> dict:
        """Эффективность использования диапазона: доля моментов наблюдения."""
        if self.store is None:
            return {}
        if threshold_db is None:
            threshold_db = self.threshold_level()
        return efficiency_from_store(self.store, threshold_db).as_dict()

    @state_locked
    def threshold_level(self) -> float:
        """Уровень линии порога, дБ; при её отсутствии — 2/3 шкалы."""
        if self.threshold is not None and self.threshold.levels.size:
            return float(np.median(self.threshold.levels))
        latest = self.store.latest() if self.store is not None else None
        if latest is None or not latest.trace.size:
            return -100.0
        return float(latest.trace.peak - 30.0)

    @state_locked
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


class RequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def finite_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Число должно быть конечным")
    return number


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

    def _stream_archive_ndjson(self, session_id: str, *, stream_id: Optional[str]) -> None:
        with open_archive_snapshot(self.sessions, session_id, stream_id=stream_id) as snapshot:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Archive-Sweeps", str(snapshot.sweep_count))
            self.send_header("X-Archive-Observations", str(snapshot.observation_count))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for index, line in enumerate(
                    iter_snapshot_ndjson(snapshot, session_id), start=1
                ):
                    self.wfile.write(line)
                    if index % 100 == 0:
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_file(self, path: Path) -> None:
        if not path.exists():
            self.send_error(404, "Not found")
            return
        body = path.read_bytes()
        # Bundled web assets must not depend on host registry MIME overrides.
        ctype = {
            ".js": "text/javascript",
            ".css": "text/css",
            ".html": "text/html",
        }.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self, *, strict: bool = False) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)

        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Поля JSON не должны повторяться")
                result[key] = value
            return result

        try:
            return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields if strict else None)
        except json.JSONDecodeError:
            if strict:
                raise ValueError("Некорректный JSON") from None
            return {}

    def _read_bounded_analysis_request(self, max_bytes: int) -> dict:
        """Bound opt-in analysis routes before reading any request bytes."""
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get_all("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]+", lengths[0])):
            raise ValueError("Нужен один корректный Content-Length без Transfer-Encoding")
        # Compare decimal strings first, so even enormous header integers are
        # rejected without int conversion limits or allocation of a body.
        digits = lengths[0].lstrip("0") or "0"
        maximum = str(max_bytes)
        if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
            raise OverflowError(f"Тело запроса превышает {max_bytes} байт")
        length = int(digits)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Неполное тело запроса")
        return strict_json_loads(raw.decode("utf-8", errors="strict"))

    # -- маршруты ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._get()
        except (ValueError, TypeError, OverflowError, UnicodeError) as exc:
            if urlparse(self.path).path.startswith("/api/v1/"):
                self._send_api_error(400, "invalid_request", str(exc))
            else:
                self._send_json({"ok": False, "error": str(exc)}, 400)

    def _get(self) -> None:
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
            if not (1 <= rows <= 100 and 1 <= cols <= 100):
                raise ValueError("Размеры сетки должны быть от 1 до 100")
            self._send_json(self.state.coverage(rows, cols))
            return
        if route == "/api/occupancy":
            raw = query.get("threshold", [None])[0]
            threshold = finite_number(raw) if raw not in (None, "") else None
            self._send_json(self.state.occupancy(threshold))
            return
        if route == "/api/efficiency":
            raw = query.get("threshold", [None])[0]
            threshold = finite_number(raw) if raw not in (None, "") else None
            self._send_json(self.state.efficiency(threshold))
            return
        if route == "/api/keywords":
            self._send_json({"keywords": CATALOG, "count": len(CATALOG)})
            return
        if route == "/api/measure":
            start = finite_number(query.get("start", ["0"])[0])
            stop = finite_number(query.get("stop", ["0"])[0])
            if stop <= start:
                raise ValueError("Конец диапазона должен быть больше начала")
            raw_norm = query.get("norm", [None])[0]
            norm = finite_number(raw_norm) if raw_norm not in (None, "") else None
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

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._post()
        except (ValueError, TypeError, OverflowError, UnicodeError) as exc:
            self.close_connection = True
            status = getattr(exc, "status", 400)
            if urlparse(self.path).path.startswith("/api/v1/"):
                self._send_api_error(status, "invalid_request", str(exc))
            else:
                self._send_json({"ok": False, "error": str(exc)}, status)

    def _post(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        parts = route.strip("/").split("/")
        # All mutating routes, including loopback v1, reject browser simple/CSRF
        # requests before reading or applying the body. Native clients omit Origin.
        if self.headers.get_content_type() != "application/json":
            raise RequestError("Требуется application/json", 415)
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin not in ("http://" + host, "https://" + host):
            raise RequestError("Чужой Origin запрещён", 403)
        if (len(parts) == 5 and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] in (
                    "sweep-mask-analysis", "sweep-average-comparison", "sweep-three-thresholds",
                    "sweep-average-change-detection",
                    "sweep-context", "candidate-frequency-rounding",
                )):
            try:
                if "?" in self.path:
                    raise ValueError("Анализ не принимает параметры запроса")
                maximum = {
                    "sweep-average-comparison": MAX_AVERAGE_REQUEST_BYTES,
                    "sweep-average-change-detection": MAX_AVERAGE_REQUEST_BYTES,
                    "sweep-mask-analysis": MAX_MASK_REQUEST_BYTES,
                    "sweep-three-thresholds": MAX_THREE_THRESHOLDS_REQUEST_BYTES,
                    "sweep-context": MAX_CONTEXT_REQUEST_BYTES,
                    "candidate-frequency-rounding": MAX_ROUNDING_REQUEST_BYTES,
                }[parts[4]]
                payload = self._read_bounded_analysis_request(maximum)
            except OverflowError as exc:
                self.close_connection = True
                self._send_api_error(413, "request_too_large", str(exc))
                return
            except (ValueError, UnicodeError) as exc:
                self.close_connection = True
                self._send_api_error(400, "invalid_request", str(exc))
                return
        elif route.startswith("/api/v1/sessions/") and route.rstrip("/").endswith("/sweep-references"):
            try:
                payload = self._read_json(strict=True)
            except (ValueError, UnicodeError) as exc:
                self._send_api_error(400, "invalid_request", str(exc))
                return
        elif not route.startswith("/api/v1/"):
            self.connection.settimeout(10)
            try:
                payload = self._read_bounded_analysis_request(65536)
            except OverflowError as exc:
                raise RequestError(str(exc), 413) from exc
            if not isinstance(payload, dict):
                raise ValueError("Ожидается объект JSON")
        else:
            payload = self._read_json()

        if route.startswith("/api/v1/"):
            self._api_v1_post(route, payload)
            return

        if route == "/api/threshold":
            with self.state._lock:
                import numpy as np
                line = self.state.threshold
                if line is None:
                    self._send_json({"ok": False, "error": "нет активной линии порога"}, 400)
                    return
                if "offset_db" in payload:
                    line = line.shifted(finite_number(payload["offset_db"]))
                if "levels" in payload and "frequencies" in payload:
                    import numpy as np

                    line = ThresholdLine(
                        np.asarray(payload["frequencies"], dtype=float),
                        np.asarray(payload["levels"], dtype=float),
                        line.index,
                        line.unit,
                    )
                if (line.frequencies.ndim != 1 or line.levels.ndim != 1 or line.empty
                        or not np.all(np.isfinite(line.frequencies)) or not np.all(np.isfinite(line.levels))
                        or np.any(np.diff(line.frequencies) <= 0)):
                    raise ValueError("Неверные частоты или уровни порога")
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
            if route == "/api/v1/archive/capacity":
                self._send_json(api_payload(self.sessions.archive_capacity()))
                return
            if route == "/api/v1/health":
                self._send_json(api_payload(self.sessions.health()))
                return
            if route == "/api/v1/capabilities":
                capabilities = self.sessions.capabilities()
                capabilities["features"].update(self.workers.capabilities())
                capabilities["features"]["session_analytics"] = True
                capabilities["features"]["session_analytics_archive_wide"] = True
                capabilities["features"]["session_archive_export"] = True
                capabilities["features"]["session_archive_export_archive_wide"] = True
                capabilities["features"]["session_archive_export_ndjson"] = True
                capabilities["features"]["session_archive_export_streaming"] = True
                capabilities["features"]["control_task_scheduler"] = True
                capabilities["features"]["control_task_scheduler_opt_in_required"] = True
                capabilities["features"]["control_task_scheduler_policy_persistent"] = False
                capabilities["features"]["control_task_scheduler_task_list"] = True
                capabilities["features"]["control_task_scheduler_task_create"] = True
                capabilities["features"]["control_task_scheduler_task_cancel"] = True
                capabilities["features"]["automatic_investigation_completion"] = True
                capabilities["features"]["automatic_investigation_completion_opt_in_required"] = True
                capabilities["features"]["automatic_investigation_completion_policy_persistent"] = False
                self._send_json(api_payload(capabilities))
                return
            if route == "/api/v1/task-scheduler":
                self._send_json(api_payload(self.control_scheduler.status()))
                return
            if route == "/api/v1/task-scheduler/tasks":
                items = self.control_scheduler.list_tasks()
                self._send_json(api_payload({"items": items, "count": len(items)}))
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
            if (len(parts) == 7 and parts[:3] == ["api", "v1", "sessions"]
                    and parts[4:6] == ["investigations", "by-id"]):
                self._send_json(api_payload(self.sessions.get_investigation(parts[3], parts[6])))
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
                and parts[4] == "automatic-completion"
            ):
                self._send_json(api_payload(self.automatic_completion.status(parts[3])))
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
                len(parts) == 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "archive"
                and parts[5] == "export.ndjson"
            ):
                stream_id = query.get("stream_id", [None])[0]
                self._stream_archive_ndjson(parts[3], stream_id=stream_id)
                return
            if (
                len(parts) == 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "archive"
                and parts[5] == "export"
            ):
                stream_id = query.get("stream_id", [None])[0]
                self._send_json(
                    api_payload(
                        build_archive_document(
                            self.sessions, parts[3], stream_id=stream_id
                        )
                    )
                )
                return
            if (
                len(parts) == 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "analytics"
                and parts[5] == "summary"
            ):
                stream_id = query.get("stream_id", [None])[0]
                self._send_json(
                    api_payload(
                        analytics_summary(self.sessions, parts[3], stream_id=stream_id)
                    )
                )
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "sweep-references"
            ):
                raw_query = urlparse(self.path).query
                if re.search(r"%(?![0-9a-fA-F]{2})", raw_query):
                    raise ValueError("Некорректное percent-кодирование параметров")
                reference_query = parse_qs(
                    raw_query, keep_blank_values=True, strict_parsing=True, errors="strict",
                ) if raw_query else {}
                if set(reference_query) - {"stream_id"} or any(
                    len(values) != 1 or not values[0].strip()
                    for values in reference_query.values()
                ):
                    raise ValueError("Разрешён только один непустой stream_id")
                self._send_json(api_payload(self.sessions.list_sweep_references(
                    parts[3], stream_id=reference_query.get("stream_id", [None])[0],
                )))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "sweep-threshold"
            ):
                raw_query = urlparse(self.path).query
                if re.search(r"%(?![0-9a-fA-F]{2})", raw_query):
                    raise ValueError("Некорректное percent-кодирование параметров")
                threshold_query = parse_qs(
                    raw_query, keep_blank_values=True, strict_parsing=True, errors="strict",
                ) if raw_query else {}
                if set(threshold_query) != {"sweep_id", "window_points", "margin_db"} or any(
                    len(values) != 1 or not values[0].strip()
                    for values in threshold_query.values()
                ):
                    raise ValueError("Нужны ровно по одному sweep_id, window_points и margin_db")
                window = threshold_query["window_points"][0]
                margin = threshold_query["margin_db"][0]
                if not re.fullmatch(r"[0-9]+", window):
                    raise ValueError("window_points должен быть нечётным целым от 3 до 101")
                if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", margin):
                    raise ValueError("margin_db должен быть конечным числом от 0 до 120 dB")
                self._send_json(api_payload(self.sessions.sweep_threshold(
                    parts[3], sweep_id=threshold_query["sweep_id"][0],
                    window_points=int(window), margin_db=float(margin),
                )))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "sweep-comparison"
            ):
                # Reparse only this new route strictly; preserve legacy query
                # behavior elsewhere. Blank and repeated values must not vanish.
                raw_query = urlparse(self.path).query
                if re.search(r"%(?![0-9a-fA-F]{2})", raw_query):
                    raise ValueError("Некорректное percent-кодирование параметров")
                comparison_query = parse_qs(
                    raw_query, keep_blank_values=True,
                    strict_parsing=True, errors="strict",
                )
                if set(comparison_query) != {"reference_id", "target_id"} or any(
                    len(values) != 1 or not values[0].strip()
                    for values in comparison_query.values()
                ):
                    raise ValueError("Нужны ровно по одному reference_id и target_id")
                self._send_json(api_payload(self.sessions.compare_sweeps(
                    parts[3], reference_id=comparison_query["reference_id"][0],
                    target_id=comparison_query["target_id"][0],
                )))
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
        except SweepNotFoundError as exc:
            self._send_api_error(404, "sweep_not_found", str(exc.args[0]))
        except ThresholdIncompatibleError as exc:
            self._send_api_error(409, "threshold_incompatible", str(exc))
        except ComparisonIncompatibleError as exc:
            self._send_api_error(409, "comparison_incompatible", str(exc))
        except TaskNotFoundError as exc:
            self._send_api_error(404, "task_not_found", str(exc.args[0]))
        except CandidateNotFoundError as exc:
            self._send_api_error(404, "candidate_not_found", str(exc.args[0]))
        except InvestigationNotFoundError as exc:
            self._send_api_error(404, "investigation_not_found", str(exc.args[0]))
        except (TypeError, ValueError, OverflowError) as exc:
            self._send_api_error(400, "invalid_request", str(exc))

    def _api_v1_post(self, route: str, payload: dict) -> None:
        try:
            if route == "/api/v1/task-scheduler":
                if set(payload) != {"enabled"}:
                    raise ValueError("Разрешено только поле enabled")
                result = self.control_scheduler.configure(payload["enabled"])
                self._send_json(api_payload(result))
                return
            if route == "/api/v1/task-scheduler/tasks":
                created = self.control_scheduler.create_task(payload)
                self._send_json(api_payload(created), 201)
                return
            if (
                route.startswith("/api/v1/task-scheduler/tasks/")
                and route.endswith("/cancel")
            ):
                name = unquote(route[len("/api/v1/task-scheduler/tasks/"):-len("/cancel")].rstrip("/"))
                result = self.control_scheduler.cancel_task(name)
                self._send_json(api_payload(result))
                return
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
                and parts[4] == "candidate-frequency-rounding"
            ):
                self._send_json(api_payload(self.sessions.candidate_frequency_rounding(parts[3], payload)))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "sweep-average-comparison"
            ):
                self._send_json(api_payload(self.sessions.sweep_average_comparison(parts[3], payload)))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "sweep-average-change-detection"
            ):
                self._send_json(api_payload(
                    self.sessions.sweep_average_change_detection(parts[3], payload)
                ))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "sweep-three-thresholds"
            ):
                self._send_json(api_payload(self.sessions.sweep_three_thresholds(parts[3], payload)))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "sweep-context"
            ):
                self._send_json(api_payload(self.sessions.sweep_context(parts[3], payload)))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "sweep-mask-analysis"
            ):
                self._send_json(api_payload(self.sessions.sweep_mask_analysis(parts[3], payload)))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] == "sweep-references"
            ):
                if urlparse(self.path).query:
                    raise ValueError("POST эталона не принимает параметры запроса")
                reference, created = self.sessions.create_sweep_reference(parts[3], payload)
                self._send_json(api_payload(reference), 201 if created else 200)
                return
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
                and parts[4] == "automatic-completion"
            ):
                result = self.automatic_completion.configure(parts[3], payload)
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
                len(parts) == 6
                and parts[:3] == ["api", "v1", "sessions"]
                and parts[4] in ("sweeps", "observations")
                and parts[5] == "batch"
            ):
                items = payload.get("items")
                if not isinstance(items, list):
                    raise TypeError("Поле items должно быть JSON-массивом")
                measurements = []
                for index, item in enumerate(items):
                    if not isinstance(item, dict):
                        raise TypeError(f"Элемент пакета {index} должен быть JSON-объектом")
                    measurement = dict(item)
                    supplied_session = measurement.get("session_id")
                    if supplied_session is not None and supplied_session != parts[3]:
                        raise ValueError(
                            f"session_id элемента {index} не совпадает с маршрутом"
                        )
                    measurement["session_id"] = parts[3]
                    measurements.append(measurement)
                if parts[4] == "sweeps":
                    result = self.sessions.ingest_sweeps(measurements)
                else:
                    result = self.sessions.ingest_observations(measurements)
                self._send_json(
                    api_payload({"items": result, "count": len(result)}), 201
                )
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
                    # Keep start admission closed until the session state is saved.
                    # Stopping workers alone leaves a concurrent-start gap here.
                    with self.workers.stopping_session(parts[3]):
                        self.workers.stop_session(parts[3])
                        session = self.sessions.stop_session(parts[3])
                self._send_json(api_payload(session))
                return
            self._send_api_error(404, "not_found", "Маршрут API v1 не найден")
        except SweepNotFoundError as exc:
            self._send_api_error(404, "sweep_not_found", str(exc.args[0]))
        except AverageReferenceIncompatibleError as exc:
            self._send_api_error(409, "average_reference_incompatible", str(exc))
        except ThreeThresholdsIncompatibleError as exc:
            self._send_api_error(409, "three_thresholds_incompatible", str(exc))
        except ContextIncompatibleError as exc:
            self._send_api_error(409, "context_incompatible", str(exc))
        except FrequencyRoundingIncompatibleError as exc:
            self._send_api_error(409, "frequency_rounding_incompatible", str(exc))
        except MaskIncompatibleError as exc:
            self._send_api_error(409, "mask_incompatible", str(exc))
        except ComparisonIncompatibleError as exc:
            self._send_api_error(409, "comparison_incompatible", str(exc))
        except ReferenceNameConflictError as exc:
            self._send_api_error(409, "reference_name_conflict", str(exc))
        except ReferenceLimitReachedError as exc:
            self._send_api_error(409, "reference_limit_reached", str(exc))
        except ArchiveCapacityExceededError as exc:
            self._send_api_error(507, "archive_capacity_exceeded", str(exc))
        except SessionNotFoundError as exc:
            self._send_api_error(404, "session_not_found", str(exc.args[0]))
        except TaskNotFoundError as exc:
            self._send_api_error(404, "task_not_found", str(exc.args[0]))
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
        except (TypeError, ValueError, OverflowError) as exc:
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
    automatic_completion: Optional[AutomaticInvestigationCompletion] = None,
    task_book: Optional[TaskBook] = None,
    task_scheduler: Optional[TaskScheduler] = None,
    archive_max_bytes: Optional[int] = None,
) -> ThreadingHTTPServer:
    """Создаёт HTTP-сервер; отдельная функция упрощает встраивание и тесты."""
    if sessions is not None and session_database is not None:
        raise ValueError("Передайте либо sessions, либо session_database, но не оба")
    if archive_max_bytes is not None and not session_database:
        raise ValueError("archive_max_bytes требует session_database")
    with ExitStack() as cleanup:
        state = ApplicationState(database=database, library=library)
        cleanup.callback(state.close)
        if sessions is not None:
            session_service = sessions
        elif session_database:
            store = SQLiteSessionStore(session_database, max_database_bytes=archive_max_bytes)
            cleanup.callback(store.close)
            session_service = SessionService(store=store)
        else:
            session_service = SessionService()
        if workers is not None and workers.sessions is not session_service:
            raise ValueError("DeviceWorkerManager должен использовать тот же SessionService")
        worker_manager = workers or DeviceWorkerManager(session_service)
        if workers is None:
            cleanup.callback(worker_manager.close)
        if automatic_handoff is not None and automatic_handoff.sessions is not session_service:
            raise ValueError("AutomaticHandoffScheduler должен использовать тот же SessionService")
        automatic_scheduler = automatic_handoff or AutomaticHandoffScheduler(
            session_service, tune=worker_manager.tune
        )
        if automatic_handoff is None:
            cleanup.callback(automatic_scheduler.close)
        if automatic_detection is not None and automatic_detection.sessions is not session_service:
            raise ValueError("AutomaticCandidateDetector должен использовать тот же SessionService")
        detection_scheduler = automatic_detection or AutomaticCandidateDetector(session_service)
        if automatic_detection is None:
            cleanup.callback(detection_scheduler.close)
        if automatic_completion is not None and automatic_completion.sessions is not session_service:
            raise ValueError("AutomaticInvestigationCompletion должен использовать тот же SessionService")
        completion_scheduler = automatic_completion or AutomaticInvestigationCompletion(
            session_service
        )
        if automatic_completion is None:
            cleanup.callback(completion_scheduler.close)
        if task_book is not None and task_scheduler is not None:
            raise ValueError("Передайте либо task_book, либо task_scheduler, но не оба")
        if task_scheduler is not None:
            control_scheduler = task_scheduler
        else:
            if task_book is None:
                task_directory = tempfile.TemporaryDirectory(prefix="openradioinspector-tasks-")
                cleanup.callback(task_directory.cleanup)
                book = TaskBook(Path(task_directory.name) / "tasks.json")
            else:
                book = task_book
            control_scheduler = TaskScheduler(book)
            cleanup.callback(control_scheduler.close)
        handler = type(
            "BoundHandler",
            (Handler,),
            {
                "state": state,
                "sessions": session_service,
                "workers": worker_manager,
                "automatic_handoff": automatic_scheduler,
                "automatic_detection": detection_scheduler,
                "automatic_completion": completion_scheduler,
                "control_scheduler": control_scheduler,
            },
        )
        httpd = ThreadingHTTPServer((host, port), handler)
        cleanup.callback(httpd.server_close)
        httpd.daemon_threads = True
        httpd.session_service = session_service
        httpd.worker_manager = worker_manager
        httpd.automatic_handoff = automatic_scheduler
        httpd.automatic_detection = detection_scheduler
        httpd.automatic_completion = completion_scheduler
        httpd.control_scheduler = control_scheduler
        resources = cleanup.pop_all()
        # Keep the original socket close inside ExitStack; the public close also
        # owns the resources constructed here, including private task storage.
        def close_owned_resources():
            if task_scheduler is None:
                control_scheduler.close()
            if automatic_completion is None:
                completion_scheduler.close()
            if automatic_detection is None:
                detection_scheduler.close()
            if automatic_handoff is None:
                automatic_scheduler.close()
            if workers is None:
                worker_manager.close()
            if worker_manager.active_count or worker_manager.pending_start_count:
                raise RuntimeError("Приборный I/O ещё выполняется; архив не закрыт")
            resources.close()
        httpd.resource_cleanup = resources
        httpd.server_close = close_owned_resources
        return httpd


def serve(
    host: str = "127.0.0.1",
    port: int = 8770,
    database: Optional[str] = None,
    library: Optional[str] = None,
    session_database: Optional[str] = None,
    task_book: Optional[TaskBook] = None,
    task_scheduler: Optional[TaskScheduler] = None,
    archive_max_bytes: Optional[int] = None,
) -> None:
    """Запуск веб-интерфейса."""
    httpd = create_server(
        host=host,
        port=port,
        database=database,
        library=library,
        session_database=session_database,
        archive_max_bytes=archive_max_bytes,
        task_book=task_book,
        task_scheduler=task_scheduler,
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
        httpd.automatic_completion.close()
        httpd.automatic_detection.close()
        httpd.automatic_handoff.close()
        httpd.control_scheduler.close()
        httpd.worker_manager.close()
        httpd.server_close()
        httpd.session_service.close()
        httpd.resource_cleanup.close()
