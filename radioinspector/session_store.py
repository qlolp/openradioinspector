"""SQLite-архив общих сеансов, событий и исходных измерений.

Одно соединение и один блокирующий писатель принадлежат ядру. Клиенты HTTP не
получают прямого доступа к базе, а модели сериализуются без преобразования
исходных единиц уровня.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from .contracts import (
    Candidate,
    Investigation,
    MonitoringSession,
    ReceiverObservation,
    SessionEvent,
    Sweep,
    ensure_utc,
    parse_time,
    to_primitive,
)
from .sweep_references import check_reference_creation
from .sweep_context import ContextIncompatibleError, validate_context_request

SCHEMA_VERSION = 5
ARCHIVE_WARNING_THRESHOLD = 0.9


class ArchiveCapacityExceededError(RuntimeError):
    """Измерение не помещается в логический лимит архива; транзакция отменена."""

    def __init__(self, database_bytes: int, max_database_bytes: int) -> None:
        self.database_bytes = database_bytes
        self.max_database_bytes = max_database_bytes
        super().__init__(
            "Достигнут лимит SQLite-архива: "
            f"{database_bytes} байт при лимите {max_database_bytes} байт. "
            "Измерения и события пакета не записаны; автоматическое удаление отключено."
        )


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

    def __init__(self, path: str, *, max_database_bytes: Optional[int] = None) -> None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Путь к базе сеансов не должен быть пустым")
        if max_database_bytes is not None and (
            isinstance(max_database_bytes, bool)
            or not isinstance(max_database_bytes, int)
            or max_database_bytes <= 0
        ):
            raise ValueError("max_database_bytes должен быть положительным целым числом")
        self.path = path
        self.max_database_bytes = max_database_bytes
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # A connection-local function, not an archive schema/index migration.
        # Unlike julianday(), this preserves microseconds and normalizes offsets
        # and legacy timestamps with omitted fractional seconds exactly.
        self._connection.create_function(
            "context_utc", 1, lambda value: _archive_time(parse_time(value)), deterministic=True,
        )
        # CAST(TEXT AS BLOB) follows the database encoding, which may be UTF-16
        # in a pre-existing archive. The contract always orders exact UTF-8.
        self._connection.create_function(
            "context_utf8", 1, lambda value: value.encode("utf-8", errors="strict"), deterministic=True,
        )
        try:
            with self._lock, self._connection:
                version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise ValueError(f"Неподдерживаемая версия архива сеансов: {version}")
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute("PRAGMA busy_timeout = 5000")
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA synchronous = NORMAL")
                # DDL and version advance are one rollback-safe migration.
                self._connection.execute("BEGIN IMMEDIATE")
                version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise ValueError(f"Неподдерживаемая версия архива сеансов: {version}")
                self._create_schema()
                self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            self._connection.close()
            raise

    @property
    def persistent(self) -> bool:
        """Whether the SQLite archive survives closing this connection."""
        return self.path != ":memory:"

    def _database_bytes_locked(self) -> int:
        # Logical main-database pages, including schema, indexes and free pages.
        # This is NOT a filesystem quota: WAL/SHM and metadata may grow further.
        page_count = self._connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = self._connection.execute("PRAGMA page_size").fetchone()[0]
        return int(page_count * page_size)

    def archive_capacity(self) -> dict[str, Any]:
        """Согласованный снимок логического объёма, без путей и размеров WAL."""
        with self._lock:
            size = self._database_bytes_locked()
            limit = self.max_database_bytes
            if limit is None:
                state = "unlimited"
            elif size >= limit:
                state = "full"
            elif size * 10 >= limit * 9:
                state = "warning"
            else:
                state = "ok"
            return {
                "storage_mode": "sqlite",
                "database_bytes": size,
                "max_database_bytes": limit,
                "remaining_bytes": None if limit is None else max(0, limit - size),
                "state": state,
                "warning_threshold": ARCHIVE_WARNING_THRESHOLD,
                "measurement_writes_blocked": state == "full",
                "automatic_deletion": False,
            }

    def _check_capacity_locked(self, *, before_write: bool) -> None:
        limit = self.max_database_bytes
        if limit is None:
            return
        size = self._database_bytes_locked()
        if size > limit or (before_write and size == limit):
            raise ArchiveCapacityExceededError(size, limit)

    @contextmanager
    def _measurement_transaction(self) -> Iterator[None]:
        with self._lock, self._connection:
            # Reserve the SQLite writer before reading capacity. Both measurements
            # and their events roll back if the post-write check rejects growth.
            self._connection.execute("BEGIN IMMEDIATE")
            self._check_capacity_locked(before_write=True)
            yield
            self._check_capacity_locked(before_write=False)

    def _create_schema(self) -> None:
        from .monitoring.schema import install_schema
        install_schema(self._connection)
        # Independent additive artifact schema; does not compete with the
        # listening/ingestion migrations of the core measurement tables.
        self._connection.execute('''CREATE TABLE IF NOT EXISTS instrument_artifacts (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, stream_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL CHECK(schema_version=1), kind TEXT NOT NULL,
            mime_type TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL, payload BLOB NOT NULL, metadata_json TEXT NOT NULL,
            FOREIGN KEY(stream_id,session_id) REFERENCES streams(id,session_id) ON DELETE CASCADE
        )''')
        self._connection.execute('''CREATE INDEX IF NOT EXISTS instrument_artifacts_scope_time
            ON instrument_artifacts(session_id,stream_id,created_at,id)''')
        schema = """
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
            CREATE INDEX IF NOT EXISTS observations_target_time ON observations (
                session_id, stream_id, frequency_hz,
                (CASE WHEN instr(started_at, '.') = 0
                    THEN replace(started_at, 'Z', '.000000Z')
                    ELSE started_at END)
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                session_id TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_session_sequence
                ON events(session_id, sequence);
            CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                source_stream_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                center_frequency_hz INTEGER NOT NULL,
                bandwidth_hz INTEGER,
                peak_level REAL,
                level_unit TEXT NOT NULL,
                score REAL,
                observation_ids_json TEXT NOT NULL,
                state TEXT NOT NULL,
                UNIQUE (id, session_id),
                FOREIGN KEY (source_stream_id, session_id)
                    REFERENCES streams(id, session_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS candidates_session_state
                ON candidates(session_id, state, started_at, id);
            CREATE TABLE IF NOT EXISTS investigations (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                target_stream_id TEXT NOT NULL,
                target_frequency_hz INTEGER NOT NULL,
                state TEXT NOT NULL,
                priority INTEGER NOT NULL,
                mode TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                result_observation_ids_json TEXT NOT NULL,
                error TEXT,
                FOREIGN KEY (candidate_id, session_id)
                    REFERENCES candidates(id, session_id) ON DELETE CASCADE,
                FOREIGN KEY (target_stream_id, session_id)
                    REFERENCES streams(id, session_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS investigations_session_queue
                ON investigations(session_id, state, priority DESC, created_at, id);
            """
        # executescript implicitly commits an open transaction. Execute these
        # fixed DDL statements individually so every migration remains atomic.
        for statement in schema.split(";"):
            if statement.strip():
                self._connection.execute(statement)
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(investigations)")}
        if "listening_started_at" not in columns:
            self._connection.execute("ALTER TABLE investigations ADD COLUMN listening_started_at TEXT")
        self._create_reference_schema()
        self._create_measurement_schema()
        from .monitoring.schema import install_sweep_index
        install_sweep_index(self._connection)

    def _create_measurement_schema(self) -> None:
        # Measurement results keep the methodology explicit and the verdict
        # code apart from the numeric values, like the original *_RESULTCODE
        # tables. Schema v4 is additive: archives from v3 migrate unchanged.
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS measurement_results (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                methodology TEXT NOT NULL,
                result_code TEXT NOT NULL CHECK (
                    result_code IN ('norm', 'not_norm', 'not_measured')
                ),
                frequency_hz INTEGER NOT NULL CHECK (
                    frequency_hz BETWEEN 0 AND 9007199254740991
                ),
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                values_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        """)
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS measurement_results_session_time
                ON measurement_results(session_id, created_at, id)
        """)
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS measurement_results_methodology
                ON measurement_results(session_id, methodology, created_at)
        """)

    def save_instrument_artifact(self, item: dict) -> None:
        with self._measurement_transaction():
            self._connection.execute('''INSERT INTO instrument_artifacts
                (id,session_id,stream_id,schema_version,kind,mime_type,filename,sha256,created_at,payload,metadata_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                tuple(item[k] for k in ['id','session_id','stream_id','schema_version','kind','mime_type','filename','sha256','created_at','payload'])+(_json(item['metadata']),))

    def get_instrument_artifact(self, session_id, stream_id, artifact_id):
        with self._lock:
            row=self._connection.execute('SELECT * FROM instrument_artifacts WHERE id=? AND session_id=? AND stream_id=?',
                                         (artifact_id,session_id,stream_id)).fetchone()
        if row is None:raise KeyError('Artifact not found')
        item=dict(row);item['metadata']=_object(item.pop('metadata_json'));item['size_bytes']=len(item['payload'])
        return item

    def list_instrument_artifacts(self,session_id,stream_id,limit=100,offset=0):
        with self._lock:
            rows=self._connection.execute('''SELECT id,session_id,stream_id,schema_version,kind,mime_type,filename,
                sha256,created_at,length(payload) AS size_bytes,metadata_json FROM instrument_artifacts
                WHERE session_id=? AND stream_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?''',
                (session_id,stream_id,limit,offset)).fetchall()
        result=[]
        for row in rows:
            item=dict(row);item['metadata']=_object(item.pop('metadata_json'));result.append(item)
        return result

    def _create_reference_schema(self) -> None:
        self._connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS sweeps_reference_owner
                ON sweeps(id, session_id, stream_id)
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS sweep_references (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                sweep_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                level_unit TEXT NOT NULL CHECK (level_unit = 'dBm'),
                points INTEGER NOT NULL CHECK (points BETWEEN 1 AND 16384),
                UNIQUE (session_id, name),
                FOREIGN KEY (sweep_id, session_id, stream_id)
                    REFERENCES sweeps(id, session_id, stream_id)
            )
        """)
        self._connection.execute("""
            CREATE INDEX IF NOT EXISTS sweep_references_session_time
                ON sweep_references(session_id, created_at, id)
        """)

    @staticmethod
    def _sweep_reference(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "session_id": row["session_id"],
            "stream_id": row["stream_id"], "sweep_id": row["sweep_id"],
            "name": row["name"], "created_at": row["created_at"],
            "interval": {"started_at": row["started_at"], "ended_at": row["ended_at"]},
            "level_unit": row["level_unit"], "points": row["points"],
        }

    def list_sweep_references(
        self, session_id: str, *, stream_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clause, parameters = self._filters(session_id, stream_id)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM sweep_references WHERE {clause} ORDER BY created_at, id",
                parameters,
            ).fetchall()
            return sorted(
                (self._sweep_reference(row) for row in rows),
                key=lambda item: (parse_time(item["created_at"]), item["id"]),
            )

    def save_sweep_reference(
        self, reference: dict[str, Any], *, event: SessionEvent,
    ) -> tuple[dict[str, Any], bool]:
        # Catalog and event metadata are not measurement writes. They remain
        # available at the measurement limit, and may grow the database.
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = check_reference_creation(
                self.list_sweep_references(reference["session_id"]),
                reference["name"], reference["sweep_id"],
            )
            if existing is not None:
                return existing, False
            self._connection.execute("""
                INSERT INTO sweep_references (
                    id, session_id, stream_id, sweep_id, name, created_at,
                    started_at, ended_at, level_unit, points
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                reference["id"], reference["session_id"], reference["stream_id"],
                reference["sweep_id"], reference["name"], reference["created_at"],
                reference["interval"]["started_at"], reference["interval"]["ended_at"],
                reference["level_unit"], reference["points"],
            ))
            self._insert_event(event)
        return reference, True

    def _write_session(
        self, session: MonitoringSession, *, event: Optional[SessionEvent] = None
    ) -> None:
        data = to_primitive(session)
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

    def save_session(self, session: MonitoringSession, *, event: Optional[SessionEvent] = None) -> None:
        with self._lock, self._connection:
            self._write_session(session, event=event)

    def _session(self, row: sqlite3.Row) -> MonitoringSession:
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
        return MonitoringSession.from_dict(payload)

    def load_sessions(self) -> list[MonitoringSession]:
        """Materialize legacy session history only on an explicit list request."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM sessions ORDER BY created_at, id"
            ).fetchall()
            return [self._session(row) for row in rows]

    def get_session(self, session_id: str) -> Optional[MonitoringSession]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._session(row) if row is not None else None

    def stream_exists(self, stream_id: str) -> bool:
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM streams WHERE id = ?", (stream_id,)
            ).fetchone() is not None

    def reset_runtime_stream_states(self) -> None:
        """OS workers never survive restart; reset without loading their history."""
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE streams SET state = 'disconnected' WHERE state != 'disconnected'"
            )

    def registry_counts(self) -> dict[str, int]:
        """Archive totals, independent of the bounded service working registry."""
        with self._lock:
            row = self._connection.execute("""
                SELECT (SELECT count(*) FROM sessions) AS sessions,
                    (SELECT count(*) FROM sessions WHERE state = 'running') AS running_sessions,
                    (SELECT count(*) FROM candidates) AS candidates,
                    (SELECT count(*) FROM investigations WHERE state = 'queued') AS queued_investigations,
                    (SELECT count(*) FROM investigations WHERE state IN ('tuning','listening','decoding')) AS active_investigations
            """).fetchone()
            return dict(row)

    def append_event(self, event: SessionEvent) -> None:
        with self._lock, self._connection:
            self._insert_event(event)

    def _insert_event(self, event: SessionEvent) -> None:
        """Вставляет событие в транзакции вызывающего метода без отдельного commit."""
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
        if event is None:
            with self._measurement_transaction():
                self._insert_sweep(sweep)
        else:
            self.record_sweeps([sweep], events=[event])

    def record_sweeps(
        self, sweeps: list[Sweep], *, events: list[SessionEvent] | tuple[SessionEvent, ...] = ()
    ) -> None:
        """Записывает пакет свипов и их события одной транзакцией."""
        if len(sweeps) != len(events):
            raise ValueError("Число свипов и событий пакета должно совпадать")
        with self._measurement_transaction():
            for sweep in sweeps:
                self._insert_sweep(sweep)
            for event in events:
                self._insert_event(event)

    def _insert_sweep(self, sweep: Sweep) -> None:
        data = to_primitive(sweep)
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
        self._connection.execute('INSERT INTO monitoring_sweep_index VALUES (?,?,?,?,?,?)',
            (sweep.id,sweep.session_id,_archive_time(sweep.interval.started_at),_archive_time(sweep.interval.ended_at),
             min(sweep.frequencies_hz),max(sweep.frequencies_hz)))

    def record_observation(
        self, observation: ReceiverObservation, *, event: Optional[SessionEvent] = None
    ) -> None:
        if event is None:
            with self._measurement_transaction():
                self._insert_observation(observation)
        else:
            self.record_observations([observation], events=[event])

    def record_observations(
        self,
        observations: list[ReceiverObservation],
        *,
        events: list[SessionEvent] | tuple[SessionEvent, ...] = (),
    ) -> None:
        """Записывает пакет наблюдений и их события одной транзакцией."""
        if len(observations) != len(events):
            raise ValueError("Число наблюдений и событий пакета должно совпадать")
        with self._measurement_transaction():
            for observation in observations:
                self._insert_observation(observation)
            for event in events:
                self._insert_event(event)

    def _insert_observation(self, observation: ReceiverObservation) -> None:
        data = to_primitive(observation)
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

    @staticmethod
    def _filters(session_id: str, stream_id: Optional[str]) -> tuple[str, list[Any]]:
        clause = "session_id = ?"
        parameters: list[Any] = [session_id]
        if stream_id is not None:
            clause += " AND stream_id = ?"
            parameters.append(stream_id)
        return clause, parameters

    @staticmethod
    def _sweep_from_row(row: sqlite3.Row) -> Sweep:
        return Sweep.from_dict(
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

    def measurement_owners(self, identifiers: tuple[str, ...]) -> dict[str, set[tuple[str, str]]]:
        """Resolve only referenced primary keys, never load a measurement archive."""
        result = {identifier: set() for identifier in identifiers}
        with self._lock:
            for offset in range(0, len(identifiers), 400):
                batch = identifiers[offset:offset + 400]
                placeholders = ",".join("?" for _ in batch)
                for table in ("sweeps", "observations"):
                    rows = self._connection.execute(
                        f"SELECT id, session_id, stream_id FROM {table} WHERE id IN ({placeholders})", batch
                    ).fetchall()
                    for row in rows:
                        result[row["id"]].add((row["session_id"], row["stream_id"]))
        return result

    def get_sweep(self, sweep_id: str, *, session_id: Optional[str] = None) -> Optional[Sweep]:
        """Read one immutable measurement via its primary key, without a scan."""
        with self._lock:
            clause = "id = ?" if session_id is None else "id = ? AND session_id = ?"
            parameters = (sweep_id,) if session_id is None else (sweep_id, session_id)
            row = self._connection.execute(
                f"SELECT * FROM sweeps WHERE {clause}", parameters
            ).fetchone()
            return None if row is None else self._sweep_from_row(row)

    def get_sweep_context(
        self, session_id: str, sweep_id: str, *, before: int, after: int,
    ) -> Optional[tuple[Sweep, list[Sweep], list[Sweep]]]:
        """Bound each side in SQL and read all three queries from one snapshot.

        Existing indexes constrain the session/stream candidates. Canonical UTC
        sorting can scan those candidates inside SQLite, but never transfers the
        entire archive to Python. No compatibility filtering precedes LIMIT.
        """
        validate_context_request({"sweep_id": sweep_id, "before": before, "after": after})
        with self._lock, self._connection:
            self._connection.execute("BEGIN")
            row = self._connection.execute(
                "SELECT * FROM sweeps WHERE id = ? AND session_id = ?", (sweep_id, session_id),
            ).fetchone()
            if row is None:
                return None
            try:
                def decode(source: sqlite3.Row) -> Sweep:
                    # Older generic readers coerce numeric-looking levels and
                    # can synthesize an ID for an empty string. A context must
                    # reject bad selected data rather than repair provenance.
                    validate_context_request({"sweep_id": source["id"], "before": 0, "after": 0})
                    levels = json.loads(source["levels_json"])
                    if (not isinstance(levels, list)
                            or any(type(value) not in (int, float) for value in levels)
                            or source["level_unit"] != "dBm"):
                        raise ContextIncompatibleError("Исходные уровни должны быть числами dBm")
                    return self._sweep_from_row(source)

                anchor = decode(row)
                timestamp = _archive_time(anchor.interval.started_at)
                binary_id = sweep_id.encode("utf-8", errors="strict")

                def neighbours(count: int, *, preceding: bool) -> list[Sweep]:
                    if count == 0:
                        return []
                    comparison, direction = ("<", "DESC") if preceding else (">", "ASC")
                    rows = self._connection.execute(
                        f"""SELECT * FROM sweeps
                            WHERE session_id = ? AND stream_id = ?
                              AND (context_utc(started_at), context_utf8(id)) {comparison} (?, ?)
                            ORDER BY context_utc(started_at) {direction}, context_utf8(id) {direction}
                            LIMIT ?""",
                        (session_id, anchor.stream_id, timestamp, binary_id, count),
                    ).fetchall()
                    if preceding:
                        rows.reverse()
                    return [decode(item) for item in rows]

                return anchor, neighbours(before, preceding=True), neighbours(after, preceding=False)
            except (TypeError, ValueError, OverflowError, RecursionError, sqlite3.OperationalError) as exc:
                raise ContextIncompatibleError("Некорректные исходные данные окна контекста") from exc

    def list_sweeps(
        self, session_id: str, *, stream_id: Optional[str] = None, limit: Optional[int] = 100
    ) -> list[Sweep]:
        clause, parameters = self._filters(session_id, stream_id)
        limit_sql = "LIMIT ?" if limit is not None else ""
        query_parameters = (*parameters, limit) if limit is not None else parameters
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM (
                    SELECT *, CASE WHEN instr(started_at, '.') = 0
                        THEN replace(started_at, 'Z', '.000000Z')
                        ELSE started_at END AS sort_started_at
                    FROM sweeps WHERE {clause}
                    ORDER BY sort_started_at DESC, id DESC {limit_sql}
                ) ORDER BY sort_started_at, id
                """,
                query_parameters,
            ).fetchall()
            return [self._sweep_from_row(row) for row in rows]

    def list_observations(
        self, session_id: str, *, stream_id: Optional[str] = None, limit: Optional[int] = 100
    ) -> list[ReceiverObservation]:
        clause, parameters = self._filters(session_id, stream_id)
        limit_sql = "LIMIT ?" if limit is not None else ""
        query_parameters = (*parameters, limit) if limit is not None else parameters
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM (
                    SELECT *, CASE WHEN instr(started_at, '.') = 0
                        THEN replace(started_at, 'Z', '.000000Z')
                        ELSE started_at END AS sort_started_at
                    FROM observations WHERE {clause}
                    ORDER BY sort_started_at DESC, id DESC {limit_sql}
                ) ORDER BY sort_started_at, id
                """,
                query_parameters,
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

    def matching_observation_ids(
        self,
        session_id: str,
        stream_id: str,
        frequency_hz: int,
        started_at: datetime,
        limit: Optional[int] = None,
        *,
        mode: Optional[str] = None,
    ) -> tuple[str, ...]:
        """ID наблюдений цели; preflight может ограничить строки явным limit."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("limit должен быть положительным целым или None")
        limit_sql = "LIMIT ?" if limit is not None else ""
        parameters: list[Any] = [
            session_id,
            stream_id,
            frequency_hz,
        ]
        mode_sql = " AND mode = ?" if mode is not None else ""
        if mode is not None:
            parameters.append(mode)
        parameters.append(_archive_time(started_at))
        if limit is not None:
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT id, started_at FROM (
                    SELECT id, started_at,
                        CASE WHEN instr(started_at, '.') = 0
                            THEN replace(started_at, 'Z', '.000000Z')
                            ELSE started_at END AS sort_started_at
                    FROM observations
                    WHERE session_id = ? AND stream_id = ? AND frequency_hz = ?{mode_sql}
                ) WHERE sort_started_at >= ?
                ORDER BY sort_started_at, id {limit_sql}
                """,
                parameters,
            ).fetchall()
        return tuple(row["id"] for row in rows)

    def save_candidate(
        self, candidate: Candidate, *, event: Optional[SessionEvent] = None
    ) -> None:
        """Сохраняет кандидата и его событие в одной транзакции."""
        with self._lock, self._connection:
            self._upsert_candidate(candidate)
            if event is not None:
                self._insert_event(event)

    def save_candidates_batch(self, items: list[tuple[Candidate, SessionEvent]]) -> None:
        """Commit a whole imported list and its creation events atomically."""
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            for candidate, event in items:
                self._upsert_candidate(candidate)
                self._insert_event(event)

    def _upsert_candidate(self, candidate: Candidate) -> None:
        data = to_primitive(candidate)
        owner = self._connection.execute(
            "SELECT session_id FROM candidates WHERE id = ?", (candidate.id,)
        ).fetchone()
        if owner is not None and owner["session_id"] != candidate.session_id:
            raise ValueError(f"Кандидат уже принадлежит другому сеансу: {candidate.id}")
        self._connection.execute(
            """
            INSERT INTO candidates (
                id, session_id, source_stream_id, started_at, ended_at,
                center_frequency_hz, bandwidth_hz, peak_level, level_unit, score,
                observation_ids_json, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                center_frequency_hz = excluded.center_frequency_hz,
                bandwidth_hz = excluded.bandwidth_hz,
                peak_level = excluded.peak_level,
                level_unit = excluded.level_unit,
                score = excluded.score,
                observation_ids_json = excluded.observation_ids_json,
                state = excluded.state
            """,
            (
                data["id"], data["session_id"], data["source_stream_id"],
                _archive_time(candidate.interval.started_at),
                _archive_time(candidate.interval.ended_at),
                data["center_frequency_hz"], data["bandwidth_hz"], data["peak_level"],
                data["level_unit"], data["score"], _json(data["observation_ids"]),
                data["state"],
            ),
        )

    def _write_investigation(
        self,
        investigation: Investigation,
        *,
        candidate: Candidate,
        event: Optional[SessionEvent] = None,
    ) -> None:
        """Атомарно сохраняет исследование, состояние кандидата и событие."""
        if investigation.candidate_id != candidate.id:
            raise ValueError("Исследование ссылается на другого кандидата")
        owner = self._connection.execute(
            "SELECT session_id FROM investigations WHERE id = ?", (investigation.id,)
        ).fetchone()
        if owner is not None and owner["session_id"] != candidate.session_id:
            raise ValueError(
                f"Исследование уже принадлежит другому сеансу: {investigation.id}"
            )
        self._upsert_candidate(candidate)
        data = to_primitive(investigation)
        self._connection.execute(
            """
            INSERT INTO investigations (
                id, session_id, candidate_id, target_stream_id,
                target_frequency_hz, state, priority, mode, created_at,
                started_at, ended_at, result_observation_ids_json, error, listening_started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                target_frequency_hz = excluded.target_frequency_hz,
                state = excluded.state,
                priority = excluded.priority,
                mode = excluded.mode,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                result_observation_ids_json = excluded.result_observation_ids_json,
                error = excluded.error,
                listening_started_at = excluded.listening_started_at
            """,
            (
                data["id"], candidate.session_id, data["candidate_id"],
                data["target_stream_id"], data["target_frequency_hz"], data["state"],
                data["priority"], data["mode"], _archive_time(investigation.created_at),
                _archive_time(investigation.started_at)
                if investigation.started_at is not None else None,
                _archive_time(investigation.ended_at)
                if investigation.ended_at is not None else None,
                _json(data["result_observation_ids"]), data["error"],
                _archive_time(investigation.listening_started_at)
                if investigation.listening_started_at is not None else None,
            ),
        )
        if event is not None:
            self._insert_event(event)

    def save_investigation(self, investigation: Investigation, *, candidate: Candidate,
                           event: Optional[SessionEvent] = None) -> None:
        with self._lock, self._connection:
            self._write_investigation(investigation, candidate=candidate, event=event)

    def save_session_stop(self, session: MonitoringSession, *, event: SessionEvent,
                          cancellations: list[tuple[Investigation, Candidate, SessionEvent]]) -> None:
        """Commit terminal session state, all cancellations and their events together."""
        # Lifecycle metadata must remain writable even when measurement capacity
        # is exhausted; stopping the service is never blocked by its own limit.
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._write_session(session, event=event)
            for investigation, candidate, cancellation_event in cancellations:
                self._write_investigation(investigation, candidate=candidate, event=cancellation_event)

    @staticmethod
    def _candidate(row: sqlite3.Row) -> Candidate:
        return Candidate.from_dict(
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "source_stream_id": row["source_stream_id"],
                "interval": {
                    "started_at": row["started_at"], "ended_at": row["ended_at"]
                },
                "center_frequency_hz": row["center_frequency_hz"],
                "bandwidth_hz": row["bandwidth_hz"],
                "peak_level": row["peak_level"],
                "level_unit": row["level_unit"],
                "score": row["score"],
                "observation_ids": json.loads(row["observation_ids_json"]),
                "state": row["state"],
            }
        )

    def get_candidate(self, candidate_id: str) -> Optional[Candidate]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return self._candidate(row) if row is not None else None

    def load_candidates(
        self, *, session_id: Optional[str] = None, state: Optional[str] = None,
    ) -> list[Candidate]:
        clauses, parameters = [], []
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM candidates" + where + " ORDER BY started_at, id", parameters,
            ).fetchall()
            return [self._candidate(row) for row in rows]

    def save_measurement_result(
        self, record: dict[str, Any], *, event: Optional[SessionEvent] = None
    ) -> None:
        """Сохраняет результат измерения и его событие в одной транзакции."""
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO measurement_results (
                    id, session_id, stream_id, methodology, result_code,
                    frequency_hz, started_at, ended_at, values_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"], record["session_id"], record["stream_id"],
                    record["methodology"], record["result_code"],
                    record["frequency_hz"],
                    record["interval"]["started_at"], record["interval"]["ended_at"],
                    _json(record["values"]), record["created_at"],
                ),
            )
            if event is not None:
                self._insert_event(event)

    def list_measurement_results(
        self, session_id: str, *, methodology: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        clause, parameters = self._filters(session_id, None)
        if methodology is not None:
            clause += " AND methodology = ?"
            parameters.append(methodology)
        query = (
            f"SELECT * FROM measurement_results WHERE {clause}"
            " ORDER BY created_at, id"
        )
        if limit is not None:
            query = (f"SELECT * FROM measurement_results WHERE {clause}"
                     " ORDER BY created_at DESC, id DESC LIMIT ?")
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
            if limit is not None:
                rows.reverse()
            return [self._measurement_result(row) for row in rows]

    @staticmethod
    def _measurement_result(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "session_id": row["session_id"],
            "stream_id": row["stream_id"], "methodology": row["methodology"],
            "result_code": row["result_code"], "frequency_hz": row["frequency_hz"],
            "interval": {
                "started_at": row["started_at"], "ended_at": row["ended_at"],
            },
            "values": _object(row["values_json"]), "created_at": row["created_at"],
        }

    def load_measurement_results(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM measurement_results ORDER BY created_at, id"
            ).fetchall()
            return [self._measurement_result(row) for row in rows]

    @staticmethod
    def _investigation(row: sqlite3.Row) -> Investigation:
        return Investigation.from_dict({
            "id": row["id"], "candidate_id": row["candidate_id"],
            "target_stream_id": row["target_stream_id"],
            "target_frequency_hz": row["target_frequency_hz"],
            "state": row["state"], "priority": row["priority"], "mode": row["mode"],
            "created_at": row["created_at"], "started_at": row["started_at"],
            "listening_started_at": row["listening_started_at"],
            "ended_at": row["ended_at"],
            "result_observation_ids": json.loads(row["result_observation_ids_json"]),
            "error": row["error"],
        })

    def get_investigation(self, investigation_id: str) -> Optional[tuple[str, Investigation]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM investigations WHERE id = ?", (investigation_id,)
            ).fetchone()
            return (row["session_id"], self._investigation(row)) if row is not None else None

    def load_open_investigations(self) -> list[tuple[str, Investigation]]:
        """Recovery loads pending work, never the growing terminal history."""
        with self._lock:
            rows = self._connection.execute("""
                SELECT * FROM investigations
                WHERE state IN ('queued','tuning','listening','decoding')
                ORDER BY created_at, id
            """).fetchall()
            return [(row["session_id"], self._investigation(row)) for row in rows]

    def load_investigations(
        self, *, session_id: Optional[str] = None, state: Optional[str] = None,
    ) -> list[tuple[str, Investigation]]:
        """Return legacy history, optionally scoped before materializing it."""
        clauses, parameters = [], []
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM investigations" + where + " ORDER BY created_at, id", parameters,
            ).fetchall()
            return [(row["session_id"], self._investigation(row)) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteSessionStore":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
