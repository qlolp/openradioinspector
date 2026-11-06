"""Read-only экспорт общего архива в версионированный JSON для кругового переноса."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .archive_ingest import (
    ARCHIVE_FORMAT,
    ARCHIVE_FORMAT_VERSION,
    ArchiveIngestClient,
    ArchiveIngestError,
    parse_archive_document,
)
from .contracts import to_primitive


def build_archive_document(
    service: Any, session_id: str, *, stream_id: Optional[str] = None
) -> dict[str, Any]:
    """Читает весь архив явно без list-limit, не создавая измерений или событий."""
    sweeps = service.list_sweeps(session_id, stream_id=stream_id, limit=None)
    observations = service.list_observations(
        session_id, stream_id=stream_id, limit=None
    )
    return {
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_FORMAT_VERSION,
        "sweeps": to_primitive(sweeps),
        "observations": to_primitive(observations),
    }


def fetch_archive_document(
    client: ArchiveIngestClient, session_id: str, *, stream_id: Optional[str] = None
) -> dict[str, Any]:
    """Получает и полностью проверяет документ до открытия выходного файла."""
    payload = client.export_archive(session_id, stream_id=stream_id)
    document = parse_archive_document(payload, session_id, allow_empty=True)
    return {
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_FORMAT_VERSION,
        "sweeps": to_primitive(document.sweeps),
        "observations": to_primitive(document.observations),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radioinspector-archive-export",
        description="Экспорт всего архива сеанса в версионированный JSON",
    )
    parser.add_argument(
        "--server", default="http://127.0.0.1:8770", help="базовый URL ядра"
    )
    parser.add_argument("--session", required=True, help="ID существующего сеанса")
    parser.add_argument("--stream-id", help="необязательный фильтр одного потока")
    parser.add_argument(
        "--output", default="-", help="выходной JSON-файл или - для stdout"
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = ArchiveIngestClient(args.server, timeout=args.timeout)
        document = fetch_archive_document(
            client, args.session, stream_id=args.stream_id
        )
        serialized = json.dumps(
            document, ensure_ascii=False, indent=2, allow_nan=False
        ) + "\n"
        if args.output == "-":
            sys.stdout.write(serialized)
        else:
            Path(args.output).write_text(serialized, encoding="utf-8")
    except (ArchiveIngestError, OSError, ValueError) as exc:
        print(f"Ошибка экспорта архива: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
