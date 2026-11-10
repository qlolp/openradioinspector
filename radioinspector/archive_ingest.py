"""Импорт внешних измерений в общий архив только через атомарный `/api/v1` batch API.

Модуль не открывает SQLite или приборные порты. Сначала проверяется весь входной
документ и фактическая конфигурация сеанса, затем записи отправляются пакетами.
Атомарен каждый пакет, а не весь много-пакетный файл.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional, TextIO
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from .contracts import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    ReceiverObservation,
    Sweep,
    to_primitive,
)

from .request_limits import MAX_MEASUREMENT_REQUEST_BYTES

ARCHIVE_FORMAT = "openradioinspector-archive"
ARCHIVE_FORMAT_VERSION = 1

MAX_RESPONSE_BYTES = 32_000_000
MAX_SERVER_BATCH_LIMIT = 1000


class ArchiveIngestError(RuntimeError):
    """Ошибка документа, HTTP-транспорта или контракта импорта."""

    def __init__(
        self, message: str, *, status: Optional[int] = None, code: str = "ingest_error"
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class ArchiveDocument:
    """Полностью проверенный документ с нормализованными доменными моделями."""

    sweeps: tuple[Sweep, ...] = ()
    observations: tuple[ReceiverObservation, ...] = ()

    @property
    def total(self) -> int:
        return len(self.sweeps) + len(self.observations)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Недопустимое JSON-число: {value}")


def _parse_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Неконечное JSON-число: {value}")
    return number


def _ensure_finite_numbers(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ArchiveIngestError(
            "Документ содержит NaN или Infinity во вложенных данных",
            code="non_finite_number",
        )
    if isinstance(value, dict):
        for item in value.values():
            _ensure_finite_numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _ensure_finite_numbers(item)


def load_archive_document(source: TextIO, session_id: str) -> ArchiveDocument:
    """Читает строгий JSON; NaN и Infinity не принимаются даже в metadata."""
    try:
        payload = json.load(
            source,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ArchiveIngestError(
            f"Входной файл не является корректным JSON: {exc}", code="invalid_json"
        ) from exc
    return parse_archive_document(payload, session_id)


def parse_archive_document(
    payload: Any, session_id: str, *, allow_empty: bool = False
) -> ArchiveDocument:
    """Валидирует весь документ до каких-либо сетевых операций записи."""
    if not isinstance(session_id, str) or not session_id:
        raise ArchiveIngestError("session_id должен быть непустой строкой", code="invalid_session")
    if not isinstance(payload, dict):
        raise ArchiveIngestError("Архивный документ должен быть JSON-объектом", code="invalid_document")
    _ensure_finite_numbers(payload)
    if payload.get("format") != ARCHIVE_FORMAT:
        raise ArchiveIngestError(
            f"Поле format должно быть {ARCHIVE_FORMAT}", code="invalid_format"
        )
    if type(payload.get("format_version")) is not int or payload.get("format_version") != ARCHIVE_FORMAT_VERSION:
        raise ArchiveIngestError(
            f"Поддерживается только format_version={ARCHIVE_FORMAT_VERSION}",
            code="unsupported_format",
        )
    allowed = {"format", "format_version", "sweeps", "observations"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ArchiveIngestError(
            f"Неизвестные поля архивного документа: {', '.join(unknown)}",
            code="invalid_document",
        )

    parsed: dict[str, tuple[Any, ...]] = {}
    for kind, model in (("sweeps", Sweep), ("observations", ReceiverObservation)):
        records = payload.get(kind, [])
        if not isinstance(records, list):
            raise ArchiveIngestError(f"Поле {kind} должно быть массивом", code="invalid_document")
        items = []
        seen_ids: set[str] = set()
        for index, record in enumerate(records):
            location = f"{kind}[{index}]"
            if not isinstance(record, dict):
                raise ArchiveIngestError(
                    f"{location} должен быть JSON-объектом", code="invalid_measurement"
                )
            measurement_id = record.get("id")
            if not isinstance(measurement_id, str) or not measurement_id.strip():
                raise ArchiveIngestError(
                    f"{location}.id обязателен для безопасного повторного импорта",
                    code="missing_id",
                )
            if measurement_id in seen_ids:
                raise ArchiveIngestError(
                    f"Дубликат ID в {kind}: {measurement_id}", code="duplicate_id"
                )
            seen_ids.add(measurement_id)
            supplied_session = record.get("session_id")
            if supplied_session is not None and supplied_session != session_id:
                raise ArchiveIngestError(
                    f"{location}.session_id не совпадает с выбранным сеансом",
                    code="session_mismatch",
                )
            normalized = dict(record)
            normalized["session_id"] = session_id
            try:
                items.append(model.from_dict(normalized))
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiveIngestError(
                    f"Некорректное измерение {location}: {exc}", code="invalid_measurement"
                ) from exc
        parsed[kind] = tuple(items)

    document = ArchiveDocument(
        sweeps=parsed["sweeps"], observations=parsed["observations"]
    )
    if document.total == 0 and not allow_empty:
        raise ArchiveIngestError("Архивный документ не содержит измерений", code="empty_document")
    return document


def _segment(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArchiveIngestError(f"{name} должен быть непустой строкой", code="invalid_id")
    return quote(value, safe="")


class ArchiveIngestClient:
    """Минимальный синхронный HTTP-клиент внешнего сборщика архива."""

    def __init__(self, base_url: str, *, timeout: float = 10.0, opener: Any = None) -> None:
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
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        max_response_bytes: Optional[int] = MAX_RESPONSE_BYTES,
    ) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = _encode_request(payload)
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            f"{self.base_url}/api/v1{path}", data=body, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = (
                    response.read()
                    if max_response_bytes is None
                    else response.read(max_response_bytes + 1)
                )
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            raise self._http_error(raw, exc.code) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ArchiveIngestError(
                f"Сервер недоступен: {reason}", code="connection_error"
            ) from exc
        if max_response_bytes is not None and len(raw) > max_response_bytes:
            raise ArchiveIngestError(
                "Ответ сервера превышает 32 МБ", status=status, code="response_too_large"
            )
        try:
            envelope = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_json_constant,
                parse_float=_parse_json_float,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ArchiveIngestError(
                "Сервер вернул некорректный JSON", status=status, code="invalid_json"
            ) from exc
        self._check_envelope(envelope, status)
        if "data" not in envelope:
            raise ArchiveIngestError(
                "В ответе отсутствует data", status=status, code="invalid_envelope"
            )
        return envelope["data"]

    @staticmethod
    def _http_error(raw: bytes, status: int) -> ArchiveIngestError:
        try:
            envelope = json.loads(raw.decode("utf-8"))
            error = envelope["error"]
            return ArchiveIngestError(
                str(error.get("message", f"HTTP {status}")),
                status=status,
                code=str(error.get("code", "server_error")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            return ArchiveIngestError(
                f"Сервер вернул HTTP {status}", status=status, code="http_error"
            )

    @staticmethod
    def _check_envelope(envelope: Any, status: int) -> None:
        if not isinstance(envelope, dict):
            raise ArchiveIngestError(
                "Ответ API должен быть объектом", status=status, code="invalid_envelope"
            )
        if envelope.get("contract") != CONTRACT_NAME:
            raise ArchiveIngestError(
                "Ответ принадлежит другому API", status=status, code="invalid_contract"
            )
        if envelope.get("contract_version") != CONTRACT_VERSION:
            raise ArchiveIngestError(
                "Неподдерживаемая версия контракта", status=status, code="unsupported_contract"
            )

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/capabilities")

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/sessions/{_segment(session_id, 'session_id')}")

    def export_archive(
        self, session_id: str, *, stream_id: Optional[str] = None
    ) -> dict[str, Any]:
        session = _segment(session_id, "session_id")
        query = f"?{urlencode({'stream_id': stream_id})}" if stream_id is not None else ""
        return self._request(
            "GET", f"/sessions/{session}/archive/export{query}", max_response_bytes=None
        )

    def ingest_batch(
        self, session_id: str, kind: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if kind not in ("sweeps", "observations"):
            raise ValueError("Неизвестный тип пакета")
        session = _segment(session_id, "session_id")
        return self._request("POST", f"/sessions/{session}/{kind}/batch", {"items": items})


def _server_batch_size(capabilities: Any, requested: Optional[int]) -> int:
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get("features"), dict):
        raise ArchiveIngestError("Capabilities не содержат features", code="invalid_capabilities")
    features = capabilities["features"]
    if features.get("measurement_batch_ingest") is not True:
        raise ArchiveIngestError("Сервер не поддерживает пакетный приём", code="batch_unsupported")
    if features.get("measurement_batch_atomic") is not True:
        raise ArchiveIngestError(
            "Сервер не гарантирует атомарность пакета", code="batch_not_atomic"
        )
    limit = features.get("measurement_batch_limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SERVER_BATCH_LIMIT:
        raise ArchiveIngestError(
            "Сервер вернул некорректный measurement_batch_limit", code="invalid_capabilities"
        )
    if requested is None:
        return limit
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise ArchiveIngestError("batch-size должен быть положительным целым", code="invalid_batch_size")
    if requested > limit:
        raise ArchiveIngestError(
            f"batch-size={requested} превышает серверный лимит {limit}", code="invalid_batch_size"
        )
    return requested


def _preflight_session(session: Any, document: ArchiveDocument, session_id: str) -> None:
    if not isinstance(session, dict) or session.get("id") != session_id:
        raise ArchiveIngestError("Сервер вернул другой сеанс", code="invalid_session")
    streams = session.get("streams")
    if not isinstance(streams, list):
        raise ArchiveIngestError("Сеанс не содержит список потоков", code="invalid_session")
    by_id = {item.get("id"): item for item in streams if isinstance(item, dict)}
    for kind, measurements in (("sweeps", document.sweeps), ("observations", document.observations)):
        for measurement in measurements:
            stream = by_id.get(measurement.stream_id)
            if stream is None:
                raise ArchiveIngestError(
                    f"{kind} {measurement.id}: поток {measurement.stream_id} не принадлежит сеансу",
                    code="foreign_stream",
                )
            if stream.get("level_unit") != measurement.level_unit:
                raise ArchiveIngestError(
                    f"{kind} {measurement.id}: единица {measurement.level_unit} не совпадает "
                    f"с потоком {stream.get('level_unit')}; автоматическое преобразование запрещено",
                    code="unit_mismatch",
                )


def _encode_request(payload: Any) -> bytes:
    """Exactly the serialization sent by ArchiveIngestClient._request."""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


BATCH_ENVELOPE_BYTES = len(_encode_request({"items": []}))


def _server_batch_max_request_bytes(capabilities: dict) -> int:
    limit = capabilities["features"].get(
        "measurement_batch_max_request_bytes", MAX_MEASUREMENT_REQUEST_BYTES
    )
    if type(limit) is not int or limit < BATCH_ENVELOPE_BYTES:
        raise ArchiveIngestError("Некорректный measurement_batch_max_request_bytes", code="invalid_capabilities")
    return min(limit, MAX_MEASUREMENT_REQUEST_BYTES)


def _batch_item_bytes(item: Any, maximum: int) -> int:
    size = len(_encode_request(to_primitive(item)))
    if BATCH_ENVELOPE_BYTES + size > maximum:
        raise ArchiveIngestError(
            f"Измерение {item.id} превышает предел тела пакета {maximum} байт",
            code="measurement_too_large",
        )
    return size


def _chunks(items: tuple[Any, ...], size: int, maximum_bytes: int):
    chunk = []
    used = BATCH_ENVELOPE_BYTES
    for item in items:
        item_bytes = _batch_item_bytes(item, maximum_bytes)
        separator = 2 if chunk else 0
        if chunk and (len(chunk) >= size or used + separator + item_bytes > maximum_bytes):
            yield chunk
            chunk = []
            used = BATCH_ENVELOPE_BYTES
            separator = 0
        chunk.append(item)
        used += separator + item_bytes
    if chunk:
        yield chunk


def import_archive(
    client: ArchiveIngestClient,
    document: ArchiveDocument,
    session_id: str,
    *,
    batch_size: Optional[int] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Выполняет preflight и импорт; при dry-run не отправляет ни одного POST."""
    capabilities = client.capabilities()
    effective_batch_size = _server_batch_size(capabilities, batch_size)
    maximum_bytes = _server_batch_max_request_bytes(capabilities)
    _preflight_session(client.get_session(session_id), document, session_id)
    # Validate every single record before the first POST, including dry runs.
    for item in (*document.sweeps, *document.observations):
        _batch_item_bytes(item, maximum_bytes)
    batches = 0
    for kind, measurements in (("sweeps", document.sweeps), ("observations", document.observations)):
        for chunk in _chunks(measurements, effective_batch_size, maximum_bytes):
            batches += 1
            if dry_run:
                continue
            items = [to_primitive(item) for item in chunk]
            response = client.ingest_batch(session_id, kind, items)
            if not isinstance(response, dict) or response.get("count") != len(items):
                raise ArchiveIngestError(
                    f"Сервер подтвердил некорректное число записей в {kind}",
                    code="invalid_batch_response",
                )
            returned = response.get("items")
            if not isinstance(returned, list) or [item.get("id") for item in returned if isinstance(item, dict)] != [item["id"] for item in items]:
                raise ArchiveIngestError(
                    f"Сервер вернул другие ID в пакете {kind}", code="invalid_batch_response"
                )
    return {
        "session_id": session_id,
        "sweeps": len(document.sweeps),
        "observations": len(document.observations),
        "batches": batches,
        "batch_size": effective_batch_size,
        "dry_run": bool(dry_run),
        "atomic_per_batch": True,
        "whole_file_atomic": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radioinspector-archive-import",
        description="Импорт внешнего JSON-архива через атомарный batch API общего ядра",
    )
    parser.add_argument(
        "--server", default="http://127.0.0.1:8770", help="базовый URL ядра"
    )
    parser.add_argument("--session", required=True, help="ID существующего сеанса")
    parser.add_argument(
        "--input", required=True, help="версионированный JSON-файл или - для stdin"
    )
    parser.add_argument(
        "--batch-size", type=int, help="размер пакета не больше лимита сервера"
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="проверить документ, capabilities и сеанс без POST-запросов",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.input == "-":
            document = load_archive_document(sys.stdin, args.session)
        else:
            with open(args.input, "r", encoding="utf-8") as source:
                document = load_archive_document(source, args.session)
        client = ArchiveIngestClient(args.server, timeout=args.timeout)
        result = import_archive(
            client,
            document,
            args.session,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    except (ArchiveIngestError, OSError, ValueError) as exc:
        print(f"Ошибка импорта архива: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
