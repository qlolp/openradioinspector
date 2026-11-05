"""SQLite-архив общих сеансов, событий и исходных измерений.

Одно соединение и один блокирующий писатель принадлежат ядру. Клиенты HTTP не
получают прямого доступа к базе, а модели сериализуются без преобразования
исходных единиц уровня.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .contracts import (
    MonitoringSession,
    ReceiverObservation,
    SessionEvent,
    Sweep,
    ensure_utc,
    parse_time,
    to_primitive,
)

SCHEMA_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(
        to_primitive(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _archive_time(value: datetime) -> str:
    return ensure_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _object(value: str) -> dict[str, Any]:
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("В архиве ожидался JSON-объект")
    return result


class SQLiteSessionStore:
    """Потокобезопасное хранилище платформы на стандартном ``sqlite3``."""

    def __init__(self, path: str) -> None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Путь к базе сеансов не должен быть пустым")
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(f"Неподдерживаемая версия архива сеансов: {version}")
            self._create_schema()
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                contract_version INTEGER NOT NULL,
                name TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS streams (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                device TEXT NOT NULL,
                role TEXT NOT NULL,
                level_unit TEXT NOT NULL,
                label TEXT NOT NULL,
                port TEXT,
                model TEXT,
                serial_number TEXT,
                firmware TEXT,
                state TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                settings_json TEXT NOT NULL,
                UNIQUE (id, session_id)
            );
            CREATE TABLE IF NOT EXISTS sweeps (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                frequencies_json TEXT NOT NULL,
                levels_json TEXT NOT NULL,
                level_unit TEXT NOT NULL,
                settings_json TEXT NOT NULL,
                FOREIGN KEY (stream_id, session_id)
                    REFERENCES streams(id, session_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS sweeps_stream_time
                ON sweeps(stream_id, started_at, id);
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                frequency_hz INTEGER NOT NULL,
                mode TEXT NOT NULL,
                level REAL,
                level_unit TEXT NOT NULL,
                squelch_open INTEGER,
                decoded_mode TEXT,
                audio_uri TEXT,
                metadata_json TEXT NOT NULL,
                FOREIGN KEY (stream_id, session_id)
                    REFERENCES streams(id, session_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS observations_stream_time
                ON observations(stream_id, started_at, id);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                session_id TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_session_sequence
                ON events(session_id, sequence);
            """
        )

    def save_session(
        self, session: MonitoringSession, *, event: Optional[SessionEvent] = None
    ) -> None:
        data = to_primitive(session)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO sessions (
                    id, contract_version, name, state, created_at,
                    started_at, ended_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    contract_version = excluded.contract_version,
                    name = excluded.name,
                    state = excluded.state,
                    created_at = excluded.created_at,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    data["id"], data["contract_version"], data["name"], data["state"],
                    data["created_at"], data["started_at"], data["ended_at"],
                    _json(data["metadata"]),
                ),
            )
            for stream in data["streams"]:
                owner = self._connection.execute(
                    "SELECT session_id FROM streams WHERE id = ?", (stream["id"],)
                ).fetchone()
                if owner is not None and owner["session_id"] != session.id:
                    raise ValueError(f"Поток уже принадлежит другому сеансу: {stream['id']}")
                self._connection.execute(
                    """
                    INSERT INTO streams (
                        id, session_id, device, role, level_unit, label, port,
                        model, serial_number, firmware, state,
                        capabilities_json, settings_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        device = excluded.device,
                        role = excluded.role,
                        level_unit = excluded.level_unit,
                        label = excluded.label,
                        port = excluded.port,
                        model = excluded.model,
                        serial_number = excluded.serial_number,
                        firmware = excluded.firmware,
                        state = excluded.state,
                        capabilities_json = excluded.capabilities_json,
                        settings_json = excluded.settings_json
                    """,
                    (
                        stream["id"], stream["session_id"], stream["device"],
                        stream["role"], stream["level_unit"], stream["label"],
                        stream["port"], stream["model"], stream["serial_number"],
                        stream["firmware"], stream["state"],
                        _json(stream["capabilities"]), _json(stream["settings"]),
                    ),
                )

            if event is not None:
                self._insert_event(event)

    def load_sessions(self) -> list[MonitoringSession]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM sessions ORDER BY created_at, id"
            ).fetchall()
            sessions = []
            for row in rows:
                streams = self._connection.execute(
                    "SELECT * FROM streams WHERE session_id = ? ORDER BY rowid", (row["id"],)
                ).fetchall()
                payload = {
                    "id": row["id"],
                    "contract_version": row["contract_version"],
                    "name": row["name"],
                    "state": row["state"],
                    "created_at": row["created_at"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "metadata": _object(row["metadata_json"]),
                    "streams": [
                        {
                            "id": stream["id"],
                            "session_id": stream["session_id"],
                            "device": stream["device"],
                            "role": stream["role"],
                            "level_unit": stream["level_unit"],
                            "label": stream["label"],
                            "port": stream["port"],
                            "model": stream["model"],
                            "serial_number": stream["serial_number"],
                            "firmware": stream["firmware"],
                            "state": stream["state"],
                            "capabilities": json.loads(stream["capabilities_json"]),
                            "settings": _object(stream["settings_json"]),
                        }
                        for stream in streams
                    ],
                }
                sessions.append(MonitoringSession.from_dict(payload))
            return sessions

    def append_event(self, event: SessionEvent) -> None:
        with self._lock, self._connection:
            self._insert_event(event)

    def _insert_event(self, event: SessionEvent) -> None:
        """Insert inside the caller's transaction; never commit independently."""
        data = to_primitive(event)
        self._connection.execute(
            """
            INSERT INTO events (sequence, event_type, occurred_at, session_id, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                data["sequence"], data["event_type"], data["occurred_at"],
                data["session_id"], _json(data["payload"]),
            ),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> SessionEvent:
        return SessionEvent(
            sequence=row["sequence"],
            event_type=row["event_type"],
            occurred_at=parse_time(row["occurred_at"]),
            session_id=row["session_id"],
            payload=_object(row["payload_json"]),
        )

    def load_events(self, *, after: int = 0, limit: int = 1000) -> list[SessionEvent]:
        """Возвращает последние события после ``after`` в порядке sequence."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM events WHERE sequence > ?
                    ORDER BY sequence DESC LIMIT ?
                ) ORDER BY sequence
                """,
                (after, limit),
            ).fetchall()
            return [self._event(row) for row in rows]

    def events(self, *, after: int = 0, limit: int = 100) -> list[SessionEvent]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?",
                (after, limit),
            ).fetchall()
            return [self._event(row) for row in rows]

    def record_sweep(
        self, sweep: Sweep, *, event: Optional[SessionEvent] = None
    ) -> None:
        data = to_primitive(sweep)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO sweeps (
                    id, session_id, stream_id, started_at, ended_at,
                    frequencies_json, levels_json, level_unit, settings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["id"], data["session_id"], data["stream_id"],
                    _archive_time(sweep.interval.started_at),
                    _archive_time(sweep.interval.ended_at),
                    _json(data["frequencies_hz"]), _json(data["levels"]),
                    data["level_unit"], _json(data["settings"]),
                ),
            )

            if event is not None:
                self._insert_event(event)

    def record_observation(
        self, observation: ReceiverObservation, *, event: Optional[SessionEvent] = None
    ) -> None:
        data = to_primitive(observation)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO observations (
                    id, session_id, stream_id, started_at, ended_at, frequency_hz,
                    mode, level, level_unit, squelch_open, decoded_mode, audio_uri,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["id"], data["session_id"], data["stream_id"],
                    _archive_time(observation.interval.started_at),
                    _archive_time(observation.interval.ended_at),
                    data["frequency_hz"], data["mode"], data["level"],
                    data["level_unit"], data["squelch_open"], data["decoded_mode"],
                    data["audio_uri"], _json(data["metadata"]),
                ),
            )

            if event is not None:
                self._insert_event(event)

    @staticmethod
    def _filters(session_id: str, stream_id: Optional[str]) -> tuple[str, list[Any]]:
        clause = "session_id = ?"
        parameters: list[Any] = [session_id]
        if stream_id is not None:
            clause += " AND stream_id = ?"
            parameters.append(stream_id)
        return clause, parameters

    def list_sweeps(
        self, session_id: str, *, stream_id: Optional[str] = None, limit: int = 100
    ) -> list[Sweep]:
        clause, parameters = self._filters(session_id, stream_id)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM (
                    SELECT *, CASE WHEN instr(started_at, '.') = 0
                        THEN replace(started_at, 'Z', '.000000Z')
                        ELSE started_at END AS sort_started_at
                    FROM sweeps WHERE {clause}
                    ORDER BY sort_started_at DESC, id DESC LIMIT ?
                ) ORDER BY sort_started_at, id
                """,
                (*parameters, limit),
            ).fetchall()
            return [
                Sweep.from_dict(
                    {
                        "id": row["id"],
                        "session_id": row["session_id"],
                        "stream_id": row["stream_id"],
                        "interval": {
                            "started_at": row["started_at"], "ended_at": row["ended_at"]
                        },
                        "frequencies_hz": json.loads(row["frequencies_json"]),
                        "levels": json.loads(row["levels_json"]),
                        "level_unit": row["level_unit"],
                        "settings": _object(row["settings_json"]),
                    }
                )
                for row in rows
            ]

    def list_observations(
        self, session_id: str, *, stream_id: Optional[str] = None, limit: int = 100
    ) -> list[ReceiverObservation]:
        clause, parameters = self._filters(session_id, stream_id)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM (
                    SELECT *, CASE WHEN instr(started_at, '.') = 0
                        THEN replace(started_at, 'Z', '.000000Z')
                        ELSE started_at END AS sort_started_at
                    FROM observations WHERE {clause}
                    ORDER BY sort_started_at DESC, id DESC LIMIT ?
                ) ORDER BY sort_started_at, id
                """,
                (*parameters, limit),
            ).fetchall()
            return [
                ReceiverObservation.from_dict(
                    {
                        "id": row["id"],
                        "session_id": row["session_id"],
                        "stream_id": row["stream_id"],
                        "interval": {
                            "started_at": row["started_at"], "ended_at": row["ended_at"]
                        },
                        "frequency_hz": row["frequency_hz"],
                        "mode": row["mode"],
                        "level": row["level"],
                        "level_unit": row["level_unit"],
                        "squelch_open": (
                            None if row["squelch_open"] is None else bool(row["squelch_open"])
                        ),
                        "decoded_mode": row["decoded_mode"],
                        "audio_uri": row["audio_uri"],
                        "metadata": _object(row["metadata_json"]),
                    }
                )
                for row in rows
            ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteSessionStore":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
