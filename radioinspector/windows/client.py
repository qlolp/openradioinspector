"""HTTP-клиент `/api/v1` для Windows GUI без доступа к приборным портам."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from ..contracts import CONTRACT_NAME, CONTRACT_VERSION

MAX_RESPONSE_BYTES = 2_000_000


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
    automatic_handoff: Optional[dict[str, Any]] = None
    automatic_detection: Optional[dict[str, Any]] = None


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
        self, method: str, path: str, payload: Optional[dict[str, Any]] = None
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
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            status = exc.code
            error = self._decode_error(raw, status)
            raise error from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise PlatformClientError(
                f"Сервер недоступен: {reason}", code="connection_error"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PlatformClientError(
                "Ответ сервера превышает 2 МБ", status=status, code="response_too_large"
            )
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlatformClientError(
                "Сервер вернул некорректный JSON", status=status, code="invalid_json"
            ) from exc
        self._check_contract(envelope, status)
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
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
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
        if envelope.get("contract_version") != CONTRACT_VERSION:
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

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/sessions/{_segment(session_id, 'session_id')}")

    def list_workers(self, session_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/workers"
        )

    def list_candidates(self, session_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/candidates"
        )

    def list_investigations(self, session_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/investigations"
        )

    def automatic_handoff(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/automatic-handoff"
        )

    def automatic_detection(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/sessions/{_segment(session_id, 'session_id')}/automatic-detection"
        )

    def snapshot(self, session_id: Optional[str] = None) -> PlatformSnapshot:
        health = self.health()
        capabilities = self.capabilities()
        sessions = self.list_sessions()
        if session_id is None:
            return PlatformSnapshot(health, capabilities, sessions)
        return PlatformSnapshot(
            health=health,
            capabilities=capabilities,
            sessions=sessions,
            session=self.get_session(session_id),
            workers=self.list_workers(session_id),
            candidates=self.list_candidates(session_id),
            investigations=self.list_investigations(session_id),
            automatic_handoff=self.automatic_handoff(session_id),
            automatic_detection=self.automatic_detection(session_id),
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
