"""Disk-backed снимки архива для потокового read-only экспорта."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .contracts import ReceiverObservation, Sweep
from .observation_precision import check_extension, decode_level


def _batch_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ValueError("batch_size должен быть целым от 1 до 1000")
    return value


def _sweep(row: sqlite3.Row) -> Sweep:
    return Sweep.from_dict(
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "stream_id": row["stream_id"],
            "interval": {
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
            },
            "frequencies_hz": json.loads(row["frequencies_json"]),
            "levels": json.loads(row["levels_json"]),
            "level_unit": row["level_unit"],
            "settings": json.loads(row["settings_json"]),
        }
    )


def _observation(row: sqlite3.Row) -> ReceiverObservation:
    return ReceiverObservation.from_dict(
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "stream_id": row["stream_id"],
            "interval": {
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
            },
            "frequency_hz": row["frequency_hz"],
            "mode": row["mode"],
            "level": decode_level(row["level"], row["level_bits_le"]),
            "level_unit": row["level_unit"],
            "squelch_open": (
                None if row["squelch_open"] is None else bool(row["squelch_open"])
            ),
            "decoded_mode": row["decoded_mode"],
            "audio_uri": row["audio_uri"],
            "metadata": json.loads(row["metadata_json"]),
        }
    )


class MemoryArchiveSnapshot:
    """Неизменная копия memory-only архива, уже находящегося в памяти процесса."""

    def __init__(
        self, sweeps: list[Sweep], observations: list[ReceiverObservation]
    ) -> None:
        self._sweeps = sweeps
        self._observations = observations
        self.sweep_count = len(sweeps)
        self.observation_count = len(observations)

    def sweeps(self, batch_size: int = 100) -> Iterator[list[Sweep]]:
        size = _batch_size(batch_size)
        for offset in range(0, len(self._sweeps), size):
            yield self._sweeps[offset : offset + size]

    def observations(self, batch_size: int = 100) -> Iterator[list[ReceiverObservation]]:
        size = _batch_size(batch_size)
        for offset in range(0, len(self._observations), size):
            yield self._observations[offset : offset + size]


class SQLiteArchiveSnapshot:
    """Читатель неизменного SQLite-файла с пакетной выборкой строк."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        stream_id: Optional[str],
    ) -> None:
        self._connection = connection
        check_extension(connection)
        self._level_bits = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ori_observation_level_bits'").fetchone() is not None
        self._clause, self._parameters = self._filters(session_id, stream_id)
        self.sweep_count = self._count("sweeps")
        self.observation_count = self._count("observations")

    @staticmethod
    def _filters(session_id: str, stream_id: Optional[str]) -> tuple[str, list[Any]]:
        clause = "session_id = ?"
        parameters: list[Any] = [session_id]
        if stream_id is not None:
            clause += " AND stream_id = ?"
            parameters.append(stream_id)
        return clause, parameters

    def _count(self, table: str) -> int:
        if table not in ("sweeps", "observations"):
            raise ValueError("Неизвестная таблица архива")
        row = self._connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {self._clause}", self._parameters
        ).fetchone()
        return int(row[0])

    def _rows(self, table: str, batch_size: int) -> Iterator[list[sqlite3.Row]]:
        size = _batch_size(batch_size)
        if table not in ("sweeps", "observations"):
            raise ValueError("Неизвестная таблица архива")
        projection, source = '*', table
        if table == 'observations':
            projection = 'source.*, precision.level_bits_le' if self._level_bits else 'source.*, NULL AS level_bits_le'
            source = '(SELECT * FROM observations WHERE ' + self._clause + ') AS source'
            if self._level_bits:
                source += ' LEFT JOIN ori_observation_level_bits AS precision ON precision.observation_id=source.id'
        cursor = self._connection.execute(
            f"""
            SELECT {projection}, CASE WHEN instr(started_at, '.') = 0
                THEN replace(started_at, 'Z', '.000000Z')
                ELSE started_at END AS sort_started_at
            FROM {source} {'' if table == 'observations' else 'WHERE ' + self._clause}
            ORDER BY sort_started_at, id
            """,
            self._parameters,
        )
        while rows := cursor.fetchmany(size):
            yield rows

    def sweeps(self, batch_size: int = 100) -> Iterator[list[Sweep]]:
        for rows in self._rows("sweeps", batch_size):
            yield [_sweep(row) for row in rows]

    def observations(self, batch_size: int = 100) -> Iterator[list[ReceiverObservation]]:
        for rows in self._rows("observations", batch_size):
            yield [_observation(row) for row in rows]


@contextmanager
def sqlite_archive_snapshot(
    store: Any, session_id: str, *, stream_id: Optional[str] = None
) -> Iterator[SQLiteArchiveSnapshot]:
    """Создаёт SQLite backup на диске и освобождает writer до чтения строк."""
    with tempfile.TemporaryDirectory(prefix="openradioinspector-export-") as folder:
        path = str(Path(folder) / "snapshot.sqlite")
        destination = sqlite3.connect(path)
        try:
            with store._lock:
                store._connection.backup(destination)
            destination.execute("PRAGMA query_only = ON")
            destination.row_factory = sqlite3.Row
            yield SQLiteArchiveSnapshot(destination, session_id, stream_id)
        finally:
            destination.close()
