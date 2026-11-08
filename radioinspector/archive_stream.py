"""Двухпроходный потоковый NDJSON-импорт с подтверждённым resume checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .archive_ingest import (
    ArchiveIngestClient,
    ArchiveIngestError,
    _ensure_finite_numbers,
    _server_batch_size,
)
from .contracts import ReceiverObservation, Sweep, to_primitive

STREAM_FORMAT = "openradioinspector-archive-ndjson"
STREAM_FORMAT_VERSION = 1
MODELS = {"sweep": Sweep, "observation": ReceiverObservation}
KIND_NAMES = {"sweep": "sweeps", "observation": "observations"}


@dataclass(frozen=True)
class StreamScan:
    sha256: str
    sweeps: int
    observations: int
    last_line: int


@dataclass(frozen=True)
class Checkpoint:
    session_id: str
    destination: str
    sha256: str
    next_line: int
    sweeps: int
    observations: int
    batches: int
    complete: bool


def _error(message: str, code: str, line: Optional[int] = None) -> ArchiveIngestError:
    location = f"строка {line}: " if line is not None else ""
    return ArchiveIngestError(f"{location}{message}", code=code)


def _payloads(
    path: Path, digest: Any = None
) -> Iterator[tuple[int, dict[str, Any]]]:
    # Preserve line terminators so the checkpoint hashes the exact snapshot bytes.
    with path.open("r", encoding="utf-8", newline="") as source:
        for line_number, raw in enumerate(source, start=1):
            if digest is not None:
                digest.update(raw.encode("utf-8"))
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise _error(f"некорректный JSON: {exc}", "invalid_json", line_number) from exc
            if not isinstance(payload, dict):
                raise _error("значение должно быть JSON-объектом", "invalid_document", line_number)
            try:
                _ensure_finite_numbers(payload)
            except ArchiveIngestError as exc:
                raise _error(str(exc), "non_finite_number", line_number) from exc
            yield line_number, payload


def _header(payload: dict[str, Any], session_id: str, line: int) -> None:
    allowed = {"format", "format_version", "session_id"}
    if set(payload) != allowed:
        raise _error("header имеет неизвестные или отсутствующие поля", "invalid_header", line)
    if payload.get("format") != STREAM_FORMAT:
        raise _error("неподдерживаемый format", "invalid_format", line)
    if type(payload.get("format_version")) is not int or payload.get("format_version") != 1:
        raise _error("поддерживается только format_version=1", "unsupported_format", line)
    if payload.get("session_id") != session_id:
        raise _error("session_id не совпадает с выбранным сеансом", "session_mismatch", line)


def _measurement(
    payload: dict[str, Any], session_id: str, line: int
) -> tuple[str, Any, str]:
    if set(payload) != {"kind", "measurement"}:
        raise _error("запись должна содержать только kind и measurement", "invalid_record", line)
    kind = payload.get("kind")
    if kind not in MODELS:
        raise _error("kind должен быть sweep или observation", "invalid_kind", line)
    record = payload.get("measurement")
    if not isinstance(record, dict):
        raise _error("measurement должен быть объектом", "invalid_measurement", line)
    measurement_id = record.get("id")
    if not isinstance(measurement_id, str) or not measurement_id.strip():
        raise _error("measurement.id обязателен", "missing_id", line)
    supplied_session = record.get("session_id")
    if supplied_session is not None and supplied_session != session_id:
        raise _error("session_id измерения не совпадает с сеансом", "session_mismatch", line)
    normalized = dict(record)
    normalized["session_id"] = session_id
    try:
        measurement = MODELS[kind].from_dict(normalized)
    except (KeyError, TypeError, ValueError) as exc:
        raise _error(f"некорректное измерение: {exc}", "invalid_measurement", line) from exc
    return kind, measurement, measurement_id


def _records(
    path: Path, session_id: str, digest: Any = None
) -> Iterator[tuple[int, str, Any, str]]:
    payloads = _payloads(path, digest=digest)
    try:
        line, header = next(payloads)
    except StopIteration as exc:
        raise ArchiveIngestError("NDJSON-файл пуст", code="empty_document") from exc
    _header(header, session_id, line)
    for line, payload in payloads:
        kind, measurement, measurement_id = _measurement(payload, session_id, line)
        yield line, kind, measurement, measurement_id


def _stream_map(session: Any, session_id: str) -> dict[str, dict[str, Any]]:
    if not isinstance(session, dict) or session.get("id") != session_id:
        raise ArchiveIngestError("Сервер вернул другой сеанс", code="invalid_session")
    streams = session.get("streams")
    if not isinstance(streams, list):
        raise ArchiveIngestError("Сеанс не содержит потоки", code="invalid_session")
    return {item["id"]: item for item in streams if isinstance(item, dict) and item.get("id")}


def _scan(path: Path, session_id: str, streams: dict[str, dict[str, Any]]) -> StreamScan:
    digest = hashlib.sha256()
    counts = {"sweep": 0, "observation": 0}
    seen = {"sweep": set(), "observation": set()}
    last_line = 1
    for line, kind, measurement, measurement_id in _records(path, session_id, digest=digest):
        last_line = line
        if measurement_id in seen[kind]:
            raise _error(f"дубликат ID {measurement_id}", "duplicate_id", line)
        seen[kind].add(measurement_id)
        stream = streams.get(measurement.stream_id)
        if stream is None:
            raise _error(f"поток {measurement.stream_id} не принадлежит сеансу", "foreign_stream", line)
        if stream.get("level_unit") != measurement.level_unit:
            raise _error("исходная единица не совпадает с потоком; преобразование запрещено", "unit_mismatch", line)
        counts[kind] += 1
    return StreamScan(digest.hexdigest(), counts["sweep"], counts["observation"], last_line)


def _load_checkpoint(path: Path) -> Checkpoint:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveIngestError(f"Некорректный checkpoint: {exc}", code="invalid_checkpoint") from exc
    required = {"session_id", "destination", "sha256", "next_line", "sweeps", "observations", "batches", "complete"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ArchiveIngestError("Checkpoint имеет неверную структуру", code="invalid_checkpoint")
    if not isinstance(payload["session_id"], str) or not payload["session_id"]:
        raise ArchiveIngestError("Checkpoint session_id некорректен", code="invalid_checkpoint")
    if not isinstance(payload["destination"], str) or not payload["destination"]:
        raise ArchiveIngestError("Checkpoint destination некорректен", code="invalid_checkpoint")
    sha256 = payload["sha256"]
    if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ArchiveIngestError("Checkpoint SHA-256 некорректен", code="invalid_checkpoint")
    for key in ("next_line", "sweeps", "observations", "batches"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ArchiveIngestError(f"Checkpoint {key} некорректен", code="invalid_checkpoint")
    if payload["next_line"] < 2 or not isinstance(payload["complete"], bool):
        raise ArchiveIngestError("Checkpoint next_line/complete некорректен", code="invalid_checkpoint")
    return Checkpoint(**payload)


def _write_checkpoint(path: Path, checkpoint: Checkpoint) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(checkpoint.__dict__, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _check_response(response: Any, kind: str, items: list[dict[str, Any]]) -> None:
    if not isinstance(response, dict) or response.get("count") != len(items):
        raise ArchiveIngestError("Сервер подтвердил неверное число записей", code="invalid_batch_response")
    returned = response.get("items")
    if not isinstance(returned, list) or [item.get("id") for item in returned if isinstance(item, dict)] != [item["id"] for item in items]:
        raise ArchiveIngestError(f"Сервер вернул другие ID в {kind}", code="invalid_batch_response")


def _import_stream_snapshot(
    client: ArchiveIngestClient,
    path: str | Path,
    session_id: str,
    *,
    batch_size: Optional[int] = None,
    checkpoint_path: Optional[str | Path] = None,
    resume: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    source = Path(path)
    checkpoint_file = Path(checkpoint_path) if checkpoint_path else None
    if resume and checkpoint_file is None:
        raise ArchiveIngestError("--resume требует --checkpoint", code="checkpoint_required")
    if resume and not checkpoint_file.exists():
        raise ArchiveIngestError("Checkpoint не существует", code="checkpoint_not_found")
    destination = client.base_url
    capabilities = client.capabilities()
    effective_batch_size = _server_batch_size(capabilities, batch_size)
    session = client.get_session(session_id)
    scan = _scan(source, session_id, _stream_map(session, session_id))

    checkpoint: Optional[Checkpoint] = None
    if checkpoint_file is not None and checkpoint_file.exists():
        if not resume:
            raise ArchiveIngestError("Checkpoint существует; для продолжения укажите --resume", code="resume_required")
        checkpoint = _load_checkpoint(checkpoint_file)
    if checkpoint is not None:
        if checkpoint.destination != destination:
            raise ArchiveIngestError("Checkpoint относится к другому узлу", code="destination_mismatch")
        if checkpoint.session_id != session_id or checkpoint.sha256 != scan.sha256:
            raise ArchiveIngestError("Checkpoint относится к другому файлу или сеансу", code="checkpoint_mismatch")

    next_line = checkpoint.next_line if checkpoint is not None else 2
    total_sweeps = checkpoint.sweeps if checkpoint is not None else 0
    total_observations = checkpoint.observations if checkpoint is not None else 0
    batches = checkpoint.batches if checkpoint is not None else 0
    if checkpoint is not None and checkpoint.complete:
        return {
            "session_id": session_id,
            "destination": destination,
            "sweeps": 0,
            "observations": 0,
            "total_sweeps": total_sweeps,
            "total_observations": total_observations,
            "batches": 0,
            "total_batches": batches,
            "next_line": next_line,
            "complete": True,
            "resumed": True,
            "dry_run": bool(dry_run),
            "atomic_per_batch": True,
            "whole_file_atomic": False,
            "sha256": scan.sha256,
        }

    imported = {"sweep": 0, "observation": 0}
    new_batches = 0
    current_kind: Optional[str] = None
    chunk: list[tuple[int, Any, str]] = []

    def flush() -> None:
        nonlocal total_sweeps, total_observations, batches, new_batches, next_line, chunk
        if not chunk:
            return
        kind = current_kind
        assert kind is not None
        items = [to_primitive(item[1]) for item in chunk]
        if not dry_run:
            response = client.ingest_batch(session_id, KIND_NAMES[kind], items)
            _check_response(response, kind, items)
        last_line = chunk[-1][0]
        imported[kind] += len(chunk)
        if kind == "sweep":
            total_sweeps += len(chunk)
        else:
            total_observations += len(chunk)
        batches += 1
        new_batches += 1
        next_line = last_line + 1
        if checkpoint_file is not None and not dry_run:
            _write_checkpoint(
                checkpoint_file,
                Checkpoint(session_id, destination, scan.sha256, next_line, total_sweeps, total_observations, batches, False),
            )
        chunk = []

    for line, kind, measurement, measurement_id in _records(source, session_id):
        if line < next_line:
            continue
        if current_kind is not None and kind != current_kind:
            flush()
        current_kind = kind
        chunk.append((line, measurement, measurement_id))
        if len(chunk) >= effective_batch_size:
            flush()
    flush()

    complete = True
    if checkpoint_file is not None and not dry_run:
        _write_checkpoint(
            checkpoint_file,
            Checkpoint(session_id, destination, scan.sha256, next_line, total_sweeps, total_observations, batches, complete),
        )
    return {
        "session_id": session_id,
        "destination": destination,
        "sweeps": imported["sweep"],
        "observations": imported["observation"],
        "total_sweeps": total_sweeps,
        "total_observations": total_observations,
        "batches": new_batches,
        "total_batches": batches,
        "next_line": next_line,
        "complete": complete and not dry_run,
        "resumed": checkpoint is not None,
        "dry_run": bool(dry_run),
        "atomic_per_batch": True,
        "whole_file_atomic": False,
        "sha256": scan.sha256,
    }


def import_stream(
    client: ArchiveIngestClient,
    path: str | Path,
    session_id: str,
    *,
    batch_size: Optional[int] = None,
    checkpoint_path: Optional[str | Path] = None,
    resume: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Снимает входной snapshot и выполняет по нему два аналитических прохода."""
    if resume and checkpoint_path is None:
        raise ArchiveIngestError("--resume требует --checkpoint", code="checkpoint_required")
    if resume and not Path(checkpoint_path).exists():
        raise ArchiveIngestError("Checkpoint не существует", code="checkpoint_not_found")
    source = Path(path)
    with tempfile.TemporaryDirectory(prefix="archive-stream-") as temporary:
        snapshot = Path(temporary) / "input.ndjson"
        with source.open("rb") as input_file, snapshot.open("wb") as output_file:
            shutil.copyfileobj(input_file, output_file)
        return _import_stream_snapshot(
            client,
            snapshot,
            session_id,
            batch_size=batch_size,
            checkpoint_path=checkpoint_path,
            resume=resume,
            dry_run=dry_run,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radioinspector-archive-stream",
        description="Двухпроходный NDJSON-импорт с resume checkpoint",
    )
    parser.add_argument("--server", default="http://127.0.0.1:8770")
    parser.add_argument("--session", required=True)
    parser.add_argument("--input", required=True, help="NDJSON-файл с header и измерениями")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--checkpoint", help="путь к checkpoint для подтверждённых пакетов")
    parser.add_argument("--resume", action="store_true", help="продолжить только существующий checkpoint")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = ArchiveIngestClient(args.server, timeout=args.timeout)
        result = import_stream(
            client,
            args.input,
            args.session,
            batch_size=args.batch_size,
            checkpoint_path=args.checkpoint,
            resume=args.resume,
            dry_run=args.dry_run,
        )
    except (ArchiveIngestError, OSError, ValueError) as exc:
        print(f"Ошибка потокового импорта: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
