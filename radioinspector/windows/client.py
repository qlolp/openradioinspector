"""HTTP-клиент `/api/v1` для Windows GUI без доступа к приборным портам."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from ..contracts import CONTRACT_NAME, CONTRACT_VERSION
from .archive_capacity import validate_archive_capacity

MAX_RESPONSE_BYTES = 2_000_000


class AverageChangeResult(dict):
    """Parsed presentation data paired with its original bounded JSON representation."""

    def __init__(self, data, data_json):
        super().__init__(data)
        self._data_json = data_json

    @property
    def data_json(self):
        return self._data_json


def _data_json(raw):
    # The entire envelope has already passed the strict decoder. Decode only its
    # top-level members to locate data, never by searching inside quoted metadata.
    decoder = json.JSONDecoder()
    index = raw.index('{') + 1
    while True:
        while raw[index].isspace(): index += 1
        key, index = decoder.raw_decode(raw, index)
        while raw[index].isspace(): index += 1
        index += 1  # colon
        while raw[index].isspace(): index += 1
        start = index
        _, index = decoder.raw_decode(raw, index)
        if key == 'data': return raw[start:index]
        while raw[index].isspace(): index += 1
        index += 1  # comma; the validated envelope contains data


class PlatformClientError(RuntimeError):
    """Ошибка транспорта, контракта или команды общего API."""

    def __init__(
        self, message: str, *, status: Optional[int] = None, code: str = "client_error"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class PlatformSnapshot:
    """Атомарный для отрисовки снимок; содержит только данные, полученные ядром."""

    health: dict[str, Any]
    capabilities: dict[str, Any]
    sessions: list[dict[str, Any]]
    session: Optional[dict[str, Any]] = None
    workers: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    investigations: list[dict[str, Any]] = field(default_factory=list)
    next_investigation: Optional[dict[str, Any]] = None
    automatic_handoff: Optional[dict[str, Any]] = None
    automatic_detection: Optional[dict[str, Any]] = None
    automatic_completion: Optional[dict[str, Any]] = None
    task_scheduler: Optional[dict[str, Any]] = None
    task_scheduler_error: Optional[str] = None
    task_scheduler_tasks: list[dict[str, Any]] = field(default_factory=list)
    task_scheduler_tasks_error: Optional[str] = None
    sweeps: list[dict[str, Any]] = field(default_factory=list)
    spectrum_stream_id: Optional[str] = None
    archive_capacity: Optional[dict[str, Any]] = None
    archive_capacity_error: Optional[str] = None


def _segment(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} должно быть непустой строкой")
    return quote(value, safe="")


def validate_tune(frequency_hz: Any, mode: Any) -> tuple[int, str]:
    """Проверяет команду до отправки; значения не переводятся из МГц или S-метра."""
    if isinstance(frequency_hz, bool) or not isinstance(frequency_hz, int):
        raise ValueError("Частота должна быть целым числом Гц")
    if frequency_hz <= 0:
        raise ValueError("Частота должна быть положительной")
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError("Режим не должен быть пустым")
    return frequency_hz, mode.strip()


class PlatformClient:
    """Небольшой синхронный клиент; GUI вызывает его только в фоновом потоке."""

    def __init__(
        self, base_url: str, *, timeout: float = 5.0, opener: Any = None
    ) -> None:
        parsed = urlsplit(str(base_url).strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Адрес сервера должен начинаться с http:// или https://")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Учётные данные в адресе сервера не поддерживаются")
        if parsed.query or parsed.fragment:
            raise ValueError("Адрес сервера не должен содержать query или fragment")
        if not 0.1 <= float(timeout) <= 120.0:
            raise ValueError("Таймаут должен быть в диапазоне 0,1…120 секунд")
        path = parsed.path.rstrip("/")
        if path.endswith("/api/v1"):
            path = path[: -len("/api/v1")]
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.build_opener()

    def _request(
        self, method: str, path: str, payload: Optional[dict[str, Any]] = None,
        *, expected_statuses: Optional[tuple[int, ...]] = None, strict_json: bool = False,
        timeout: Optional[float] = None, exact_envelope: bool = False, preserve_data_json: bool = False
    ) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            f"{self.base_url}/api/v1{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout if timeout is None else min(self.timeout, timeout)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            status = exc.code
            if len(raw) > MAX_RESPONSE_BYTES:
                raise PlatformClientError("Ответ сервера превышает 2 МБ", status=status, code="response_too_large") from exc
            error = self._decode_error(raw, status)
            raise error from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise PlatformClientError(
                f"Сервер недоступен: {reason}", code="connection_error"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PlatformClientError("Ответ сервера превышает 2 МБ", status=status, code="response_too_large")
        if expected_statuses is not None and status not in expected_statuses:
            raise PlatformClientError(
                f"Неожиданный HTTP {status}; результат запроса не подтверждён",
                status=status, code="unexpected_status",
            )
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError("Duplicate JSON key")
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError("Non-finite JSON number")
        try:
            envelope = json.loads(raw.decode("utf-8"), **({"object_pairs_hook": unique_object, "parse_constant": invalid_constant} if strict_json else {}))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PlatformClientError(
                "Сервер вернул некорректный JSON", status=status, code="invalid_json"
            ) from exc
        self._check_contract(envelope, status)
        if exact_envelope and set(envelope) != {'contract', 'contract_version', 'data'}:
            raise PlatformClientError(
                "Некорректные поля ответа API", status=status, code="invalid_envelope"
            )
        if "error" in envelope:
            error = envelope["error"]
            raise PlatformClientError(
                str(error.get("message", "Ошибка сервера")),
                status=status,
                code=str(error.get("code", "server_error")),
            )
        if "data" not in envelope:
            raise PlatformClientError(
                "В ответе отсутствует data", status=status, code="invalid_envelope"
            )
        if preserve_data_json and isinstance(envelope["data"], dict):
            return AverageChangeResult(envelope["data"], _data_json(raw.decode("utf-8")))
        return envelope["data"]

    @staticmethod
    def _decode_error(raw: bytes, status: int) -> PlatformClientError:
        try:
            envelope = json.loads(raw.decode("utf-8"))
            error = envelope["error"]
            return PlatformClientError(
                str(error.get("message", f"HTTP {status}")),
                status=status,
                code=str(error.get("code", "server_error")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError, RecursionError):
            return PlatformClientError(
                f"Сервер вернул HTTP {status}", status=status, code="http_error"
            )

    @staticmethod
    def _check_contract(envelope: Any, status: int) -> None:
        if not isinstance(envelope, dict):
            raise PlatformClientError(
                "Ответ API должен быть объектом", status=status, code="invalid_envelope"
            )
        if envelope.get("contract") != CONTRACT_NAME:
            raise PlatformClientError(
                "Ответ принадлежит другому API", status=status, code="invalid_contract"
            )
        if type(envelope.get("contract_version")) is not int or envelope.get("contract_version") != CONTRACT_VERSION:
            raise PlatformClientError(
                "Неподдерживаемая версия контракта",
                status=status,
                code="unsupported_contract",
            )

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/capabilities")

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/sessions")

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/sessions", payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/sessions/{_segment(session_id, 'session_id')}")

    def list_workers(self, session_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/workers"
        )

    def compare_sweeps(self, session_id: str, reference_id: str, target_id: str) -> dict[str, Any]:
        query = urlencode({"reference_id": reference_id, "target_id": target_id})
        return self._request("GET", f"/sessions/{_segment(session_id, 'session_id')}/sweep-comparison?{query}")

    def compare_average_sweeps(self, session_id: str, reference_ids: list[str], target_id: str) -> dict[str, Any]:
        from .sweep_average import validate_ids
        validate_ids(reference_ids, target_id)
        payload = {'reference_ids': reference_ids, 'target_id': target_id}
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > 8192:
            raise ValueError('Запрос превышает 8192 байт')
        return self._request('POST', f"/sessions/{_segment(session_id, 'session_id')}/sweep-average-comparison", payload, expected_statuses=(200,))

    def detect_average_sweep_changes(
        self, session_id: str, reference_ids: list[str], target_id: str,
        minimum_increase_db: float, minimum_bins: int,
    ) -> dict[str, Any]:
        from .sweep_average import validate_change_parameters, validate_ids
        from .sweep_context import scope_identity
        if not scope_identity(session_id): raise ValueError('Некорректный session_id')
        validate_ids(reference_ids, target_id)
        minimum_increase_db, minimum_bins = validate_change_parameters(minimum_increase_db, minimum_bins)
        payload = {
            'reference_ids': reference_ids, 'target_id': target_id,
            'minimum_increase_db': minimum_increase_db, 'minimum_bins': minimum_bins,
        }
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 8192:
            raise ValueError('Запрос превышает 8192 байт')
        return self._request(
            'POST', f"/sessions/{_segment(session_id, 'session_id')}/sweep-average-change-detection",
            payload, expected_statuses=(200,), strict_json=True, timeout=8.0, exact_envelope=True, preserve_data_json=True,
        )

    def sweep_context(self, session_id: str, sweep_id: str, before: int = 2, after: int = 2) -> dict[str, Any]:
        from .sweep_average import identity
        from .sweep_context import scope_identity, validate_counts
        if not scope_identity(session_id) or not identity(sweep_id): raise ValueError('Некорректный ID')
        validate_counts(before, after)
        payload = {'sweep_id': sweep_id, 'before': before, 'after': after}
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 8192:
            raise ValueError('Запрос превышает 8192 байта')
        return self._request('POST', f"/sessions/{_segment(session_id, 'session_id')}/sweep-context", payload, expected_statuses=(200,), strict_json=True, timeout=8.0)

    def sweep_three_thresholds(self, session_id: str, sweep_id: str, levels_dbm: list[float]) -> dict[str, Any]:
        from .sweep_average import identity
        from .sweep_three_thresholds import validate_levels
        if not identity(sweep_id): raise ValueError('Некорректный sweep_id')
        validate_levels(levels_dbm)
        payload = {'sweep_id': sweep_id, 'levels_dbm': levels_dbm}
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 8192:
            raise ValueError('Запрос превышает 8192 байта')
        return self._request('POST', f"/sessions/{_segment(session_id, 'session_id')}/sweep-three-thresholds", payload, expected_statuses=(200,))

    def sweep_mask_analysis(self, session_id: str, sweep_id: str, mask_json: str) -> dict[str, Any]:
        from .spectral_mask import check_draft
        check_draft(mask_json)
        _segment(sweep_id, 'sweep_id')
        payload = {'sweep_id': sweep_id, 'mask_json': mask_json}
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > 524288:
            raise ValueError('Запрос превышает 524 288 байт')
        return self._request('POST', f"/sessions/{_segment(session_id, 'session_id')}/sweep-mask-analysis", payload, expected_statuses=(200,))

    def candidate_frequency_rounding(self, session_id: str, candidate_id: str, profile_json: str) -> dict[str, Any]:
        from .frequency_rounding import check_draft, identity
        if not identity(session_id) or not identity(candidate_id, 128):
            raise ValueError('Некорректный ID сеанса или кандидата')
        check_draft(profile_json)
        payload = {'candidate_id': candidate_id, 'profile_json': profile_json}
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 524288:
            raise ValueError('Запрос превышает 524 288 байт')
        return self._request('POST', f"/sessions/{_segment(session_id, 'session_id')}/candidate-frequency-rounding",
                             payload, expected_statuses=(200,), strict_json=True, timeout=8.0, exact_envelope=True)

    def sweep_threshold(self, session_id: str, sweep_id: str, window_points: int = 21, margin_db: float = 6) -> dict[str, Any]:
        from .sweep_threshold import validate_parameters
        validate_parameters(window_points, margin_db)
        _segment(sweep_id, "sweep_id")
        query = urlencode({"sweep_id": sweep_id, "window_points": window_points, "margin_db": margin_db})
        return self._request("GET", f"/sessions/{_segment(session_id, 'session_id')}/sweep-threshold?{query}", expected_statuses=(200,))

    def list_sweep_references(self, session_id: str, stream_id: Optional[str] = None) -> dict[str, Any]:
        path = f"/sessions/{_segment(session_id, 'session_id')}/sweep-references"
        if stream_id is not None:
            _segment(stream_id, "stream_id")
            path += "?" + urlencode({"stream_id": stream_id})
        return self._request("GET", path, expected_statuses=(200,))

    def save_sweep_reference(self, session_id: str, name: str, sweep_id: str) -> dict[str, Any]:
        from .sweep_references import normalize_name
        _segment(sweep_id, "sweep_id")
        return self._request("POST", f"/sessions/{_segment(session_id, 'session_id')}/sweep-references",
                             {"name": normalize_name(name), "sweep_id": sweep_id}, expected_statuses=(200, 201))

    def list_sweeps(
        self, session_id: str, stream_id: str, *, limit: int = 40
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Лимит sweeps должен быть целым в диапазоне 1…1000")
        session = _segment(session_id, "session_id")
        _segment(stream_id, "stream_id")
        query = urlencode({"stream_id": stream_id, "limit": limit})
        return self._request("GET", f"/sessions/{session}/sweeps?{query}")

    def list_candidates(self, session_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/candidates"
        )

    def create_candidate(
        self, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        return self._post(f"/sessions/{session}/candidates", payload)

    def dismiss_candidate(
        self, session_id: str, candidate_id: str
    ) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        candidate = _segment(candidate_id, "candidate_id")
        return self._post(f"/sessions/{session}/candidates/{candidate}/dismiss")

    def enqueue_investigation(
        self, session_id: str, candidate_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        candidate = _segment(candidate_id, "candidate_id")
        return self._post(
            f"/sessions/{session}/candidates/{candidate}/investigations", payload
        )

    def list_investigations(self, session_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/investigations"
        )

    def next_investigation(self, session_id: str) -> Optional[dict[str, Any]]:
        session = _segment(session_id, "session_id")
        return self._request("GET", f"/sessions/{session}/investigations/next")

    def start_next_investigation(
        self, session_id: str, investigation_id: str, *, confirm: bool
    ) -> dict[str, Any]:
        if confirm is not True:
            raise ValueError("Полуавтоматический handoff требует явного confirm=true")
        session = _segment(session_id, "session_id")
        _segment(investigation_id, "investigation_id")
        return self._post(
            f"/sessions/{session}/investigations/start-next",
            {"investigation_id": investigation_id, "confirm": True},
        )

    def _investigation_action(
        self, session_id: str, investigation_id: str, action: str
    ) -> str:
        session = _segment(session_id, "session_id")
        investigation = _segment(investigation_id, "investigation_id")
        return f"/sessions/{session}/investigations/{investigation}/{action}"

    def start_investigation(
        self, session_id: str, investigation_id: str
    ) -> dict[str, Any]:
        return self._post(self._investigation_action(session_id, investigation_id, "start"))

    def complete_investigation(
        self, session_id: str, investigation_id: str
    ) -> dict[str, Any]:
        return self._post(
            self._investigation_action(session_id, investigation_id, "complete")
        )

    def fail_investigation(
        self, session_id: str, investigation_id: str, *, error: str
    ) -> dict[str, Any]:
        if not isinstance(error, str) or not error.strip():
            raise ValueError("Причина сбоя не должна быть пустой")
        return self._post(
            self._investigation_action(session_id, investigation_id, "fail"),
            {"error": error.strip()},
        )

    def cancel_investigation(
        self, session_id: str, investigation_id: str
    ) -> dict[str, Any]:
        return self._post(
            self._investigation_action(session_id, investigation_id, "cancel")
        )

    def automatic_handoff(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/automatic-handoff"
        )

    def automatic_detection(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/automatic-detection"
        )

    def configure_automatic_handoff(
        self, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        return self._post(f"/sessions/{session}/automatic-handoff", payload)

    def configure_automatic_detection(
        self, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        return self._post(f"/sessions/{session}/automatic-detection", payload)

    def automatic_completion(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/automatic-completion"
        )

    def configure_automatic_completion(
        self, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        return self._post(f"/sessions/{session}/automatic-completion", payload)

    def archive_capacity(self) -> dict[str, Any]:
        data = self._request("GET", "/archive/capacity")
        try:
            return validate_archive_capacity(data)
        except ValueError as exc:
            raise PlatformClientError(str(exc), code="invalid_archive_capacity") from exc

    def task_scheduler(self) -> dict[str, Any]:
        return self._request("GET", "/task-scheduler")

    def task_scheduler_tasks(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/task-scheduler/tasks")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise PlatformClientError(
                "В ответе отсутствует список ControlTask", code="invalid_task_list"
            )
        items = data["items"]
        if (
            isinstance(data.get("count"), bool)
            or not isinstance(data.get("count"), int)
            or data["count"] != len(items)
            or any(not isinstance(item, dict) for item in items)
        ):
            raise PlatformClientError(
                "Список ControlTask имеет некорректный контракт",
                code="invalid_task_list",
            )
        return items

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/task-scheduler/tasks", payload)

    def cancel_task(self, name: str) -> dict[str, Any]:
        task = _segment(name, "name")
        return self._post(f"/task-scheduler/tasks/{task}/cancel", {})

    def configure_task_scheduler(self, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise TypeError("enabled должен быть JSON boolean")
        return self._post("/task-scheduler", {"enabled": enabled})

    def analytics_summary(self, session_id: str) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        return self._request("GET", f"/sessions/{session}/analytics/summary")

    def export_archive(self, session_id: str, output: str) -> dict[str, Any]:
        """Атомарно сохраняет серверный NDJSON snapshot через существующий verifier."""
        from ..archive_export_stream import fetch_archive_ndjson

        return fetch_archive_ndjson(self, session_id, output)

    def snapshot(
        self,
        session_id: Optional[str] = None,
        *,
        spectrum_stream_id: Optional[str] = None,
    ) -> PlatformSnapshot:
        if session_id is None and spectrum_stream_id is not None:
            raise ValueError("Для sweeps необходимо выбрать сеанс")
        health = self.health()
        capabilities = self.capabilities()
        sessions = self.list_sessions()
        capacity = None
        capacity_error = None
        try:
            capacity = self.archive_capacity()
        except PlatformClientError as exc:
            capacity_error = str(exc)
        scheduler_status = None
        scheduler_error = None
        scheduler_tasks: list[dict[str, Any]] = []
        scheduler_tasks_error = None
        try:
            scheduler_status = self.task_scheduler()
        except PlatformClientError as exc:
            scheduler_error = str(exc)
        try:
            scheduler_tasks = self.task_scheduler_tasks()
        except PlatformClientError as exc:
            scheduler_tasks_error = str(exc)
        if session_id is None:
            return PlatformSnapshot(
                health,
                capabilities,
                sessions,
                archive_capacity=capacity,
                archive_capacity_error=capacity_error,
                task_scheduler=scheduler_status,
                task_scheduler_error=scheduler_error,
                task_scheduler_tasks=scheduler_tasks,
                task_scheduler_tasks_error=scheduler_tasks_error,
            )
        sweeps = (
            self.list_sweeps(session_id, spectrum_stream_id, limit=40)
            if spectrum_stream_id is not None
            else []
        )
        return PlatformSnapshot(
            health=health,
            capabilities=capabilities,
            sessions=sessions,
            session=self.get_session(session_id),
            workers=self.list_workers(session_id),
            candidates=self.list_candidates(session_id),
            investigations=self.list_investigations(session_id),
            next_investigation=self.next_investigation(session_id),
            automatic_handoff=self.automatic_handoff(session_id),
            automatic_detection=self.automatic_detection(session_id),
            automatic_completion=self.automatic_completion(session_id),
            archive_capacity=capacity,
            archive_capacity_error=capacity_error,
            task_scheduler=scheduler_status,
            task_scheduler_error=scheduler_error,
            task_scheduler_tasks=scheduler_tasks,
            task_scheduler_tasks_error=scheduler_tasks_error,
            sweeps=sweeps,
            spectrum_stream_id=spectrum_stream_id,
        )

    def _post(self, path: str, payload: Optional[dict[str, Any]] = None) -> Any:
        return self._request("POST", path, payload if payload is not None else {})

    def start_session(self, session_id: str) -> dict[str, Any]:
        return self._post(f"/sessions/{_segment(session_id, 'session_id')}/start")

    def stop_session(self, session_id: str) -> dict[str, Any]:
        return self._post(f"/sessions/{_segment(session_id, 'session_id')}/stop")

    def start_workers(self, session_id: str) -> list[dict[str, Any]]:
        return self._post(f"/sessions/{_segment(session_id, 'session_id')}/workers/start")

    def stop_workers(self, session_id: str) -> list[dict[str, Any]]:
        return self._post(f"/sessions/{_segment(session_id, 'session_id')}/workers/stop")

    def start_stream(self, session_id: str, stream_id: str) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        stream = _segment(stream_id, "stream_id")
        return self._post(f"/sessions/{session}/streams/{stream}/start")

    def stop_stream(self, session_id: str, stream_id: str) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        stream = _segment(stream_id, "stream_id")
        return self._post(f"/sessions/{session}/streams/{stream}/stop")

    def tune(
        self, session_id: str, stream_id: str, *, frequency_hz: int, mode: str
    ) -> dict[str, Any]:
        frequency, selected_mode = validate_tune(frequency_hz, mode)
        session = _segment(session_id, "session_id")
        stream = _segment(stream_id, "stream_id")
        return self._post(
            f"/sessions/{session}/streams/{stream}/tune",
            {"frequency_hz": frequency, "mode": selected_mode},
        )
