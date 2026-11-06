"""Потоковый read-only NDJSON-экспорт с disk-backed SQLite snapshot."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .archive_ingest import ArchiveIngestClient, ArchiveIngestError, _segment
from .archive_snapshot import MemoryArchiveSnapshot, sqlite_archive_snapshot
from .archive_stream import (
    STREAM_FORMAT,
    STREAM_FORMAT_VERSION,
    _scan,
    _stream_map,
)
from .contracts import to_primitive


@contextmanager
def open_archive_snapshot(
    service: Any, session_id: str, *, stream_id: Optional[str] = None
) -> Iterator[Any]:
    """Фиксирует архив вне writer-контура SessionService."""
    with service._lock:
        service._validate_stream_query_locked(session_id, stream_id)
        if service._store is None:
            snapshot = MemoryArchiveSnapshot(
                *service.archive_measurements(session_id, stream_id=stream_id)
            )
        else:
            snapshot = None
    if snapshot is not None:
        yield snapshot
        return
    with sqlite_archive_snapshot(
        service._store, session_id, stream_id=stream_id
    ) as snapshot:
        yield snapshot


def iter_archive_ndjson(
    service: Any,
    session_id: str,
    *,
    stream_id: Optional[str] = None,
    batch_size: int = 100,
) -> Iterator[bytes]:
    """Сериализует один snapshot построчно без сборки документа в памяти."""
    with open_archive_snapshot(service, session_id, stream_id=stream_id) as snapshot:
        yield from iter_snapshot_ndjson(snapshot, session_id, batch_size=batch_size)


def iter_snapshot_ndjson(
    snapshot: Any, session_id: str, *, batch_size: int = 100
) -> Iterator[bytes]:
    """Сериализует ранее открытый snapshot и его точные счётчики."""
    header = {
        "format": STREAM_FORMAT,
        "format_version": STREAM_FORMAT_VERSION,
        "session_id": session_id,
    }
    yield _line(header)
    for batch in snapshot.sweeps(batch_size):
        for item in batch:
            yield _line({"kind": "sweep", "measurement": to_primitive(item)})
    for batch in snapshot.observations(batch_size):
        for item in batch:
            yield _line({"kind": "observation", "measurement": to_primitive(item)})


def _line(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _count_header(response: Any, name: str) -> int:
    value = response.headers.get(name)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ArchiveIngestError(
            f"Ответ не содержит корректный {name}", code="invalid_stream_response"
        ) from exc
    if result < 0 or str(result) != value:
        raise ArchiveIngestError(
            f"Ответ не содержит корректный {name}", code="invalid_stream_response"
        )
    return result


def fetch_archive_ndjson(
    client: ArchiveIngestClient,
    session_id: str,
    output: str | Path,
    *,
    stream_id: Optional[str] = None,
) -> dict[str, Any]:
    """Проверяет весь поток и только затем атомарно заменяет выходной файл."""
    destination = str(output)
    parent = (
        Path.cwd()
        if destination == "-"
        else Path(destination).expanduser().resolve().parent
    )
    if not parent.is_dir():
        raise ArchiveIngestError("Каталог выходного файла не существует", code="invalid_output")
    path = f"/sessions/{_segment(session_id, 'session_id')}/archive/export.ndjson"
    if stream_id is not None:
        from urllib.parse import urlencode

        path += "?" + urlencode({"stream_id": stream_id})
    request = urllib.request.Request(
        f"{client.base_url}/api/v1{path}",
        headers={"Accept": "application/x-ndjson"},
        method="GET",
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix=".archive-export-", dir=parent
        ) as folder:
            temporary = Path(folder) / "archive.ndjson"
            with client._opener.open(request, timeout=client.timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith("application/x-ndjson"):
                    raise ArchiveIngestError(
                        "Сервер вернул не NDJSON", code="invalid_stream_response"
                    )
                expected_sweeps = _count_header(response, "X-Archive-Sweeps")
                expected_observations = _count_header(
                    response, "X-Archive-Observations"
                )
                with temporary.open("wb") as target:
                    shutil.copyfileobj(response, target, length=256 * 1024)
            session = client.get_session(session_id)
            scan = _scan(
                temporary,
                session_id,
                _stream_map(session, session_id),
            )
            if (scan.sweeps, scan.observations) != (
                expected_sweeps,
                expected_observations,
            ):
                raise ArchiveIngestError(
                    "Число записей не совпадает с headers snapshot",
                    code="incomplete_stream",
                )
            if destination == "-":
                with temporary.open("r", encoding="utf-8") as source:
                    shutil.copyfileobj(source, sys.stdout)
            else:
                temporary.replace(destination)
            return {
                "session_id": session_id,
                "stream_id": stream_id,
                "sweeps": scan.sweeps,
                "observations": scan.observations,
                "sha256": scan.sha256,
                "archive_wide": True,
                "snapshot": "immutable",
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read(1_000_001)
        code = "http_error"
        message = f"HTTP {exc.code}"
        try:
            payload = json.loads(raw)
            detail = payload.get("error", {})
            code = detail.get("code", code)
            message = detail.get("message", message)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise ArchiveIngestError(message, status=exc.code, code=code) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise ArchiveIngestError(
            f"Ошибка потокового экспорта: {exc}", code="connection_error"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radioinspector-archive-export-stream",
        description="Потоковый экспорт всего архива сеанса в NDJSON",
    )
    parser.add_argument(
        "--server", default="http://127.0.0.1:8770", help="базовый URL ядра"
    )
    parser.add_argument("--session", required=True, help="ID существующего сеанса")
    parser.add_argument("--stream-id", help="необязательный фильтр одного потока")
    parser.add_argument(
        "--output", default="-", help="выходной NDJSON-файл или - для stdout"
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = ArchiveIngestClient(args.server, timeout=args.timeout)
        result = fetch_archive_ndjson(
            client,
            args.session,
            args.output,
            stream_id=args.stream_id,
        )
        if args.output != "-":
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except (ArchiveIngestError, OSError, ValueError) as exc:
        print(f"Ошибка потокового экспорта: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
