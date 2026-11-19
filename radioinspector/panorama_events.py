"""Create a compact panorama archive or materialize one as a fresh schema-6 copy."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .contracts import parse_time

VERSION = 7
COLUMN = "ori_sweep_ref_v1"
INSTALL = "ALTER TABLE events ADD COLUMN ori_sweep_ref_v1 TEXT REFERENCES sweeps(id)"
INLINE = "SELECT e.* FROM events e"
JOINED = """SELECT e.*, s.session_id AS source_session, s.stream_id AS source_stream,
    s.started_at AS source_start, s.ended_at AS source_end, s.level_unit AS source_unit,
    s.frequencies_json AS source_hz, s.levels_json AS source_levels
    FROM events e LEFT JOIN sweeps s ON s.id=e.ori_sweep_ref_v1"""
REFERENCES = "(e.ori_sweep_ref_v1 IS NOT NULL OR substr(ltrim(e.payload_json),1,1)='[')"


def check_schema(db, version):
    marked = any(row[1] == COLUMN for row in db.execute("PRAGMA table_info(events)"))
    if version > VERSION or marked != (version == VERSION):
        raise ValueError("Unsupported archive schema or missing panorama event version marker")
    if marked and not any(row[2:5] == ("sweeps", COLUMN, "id")
                          for row in db.execute("PRAGMA foreign_key_list(events)")):
        raise ValueError("Invalid panorama event foreign key")
    return marked


def writer_format(db, legacy_max, opt_in):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    compact = check_schema(db, version)
    if version > legacy_max and not compact:
        raise ValueError(f"Неподдерживаемая версия архива сеансов: {version}")
    initialize = opt_in and not compact
    if initialize and (version or db.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()):
        raise ValueError("Compact initialization requires an empty archive")
    return version, compact, initialize


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _digest(frequencies, levels):
    digest = hashlib.sha256(frequencies.encode("utf-8"))
    digest.update(b"\0")
    digest.update(levels.encode("utf-8"))
    return digest.hexdigest()


def encode(event, source, hz, levels):
    """The caller has inserted the source and owns the single writer transaction."""
    body = event["payload"]
    sweep = body.get("sweep")
    if not isinstance(sweep, dict) or "levels" not in sweep or "frequencies_hz" not in sweep:
        return None  # Custom full events do not depend on an archive row.
    if (event["session_id"] != source.session_id or sweep.get("id") != source.id
            or sweep.get("session_id") != source.session_id or sweep.get("stream_id") != source.stream_id
            or sweep.get("level_unit") != source.level_unit
            or parse_time(sweep["interval"]["started_at"]) != source.interval.started_at
            or parse_time(sweep["interval"]["ended_at"]) != source.interval.ended_at
            or _json(sweep["frequencies_hz"]) != hz or _json(sweep["levels"]) != levels):
        raise ValueError("Panorama event disagrees with its source sweep")
    residual = {key: value for key, value in sweep.items() if key not in ("frequencies_hz", "levels")}
    return _json([_digest(hz, levels), {**body, "sweep": residual}])


def payload(row):
    value = json.loads(row["payload_json"])
    reference = row[COLUMN] if COLUMN in row.keys() else None
    if reference is None:
        if not isinstance(value, dict):
            raise ValueError("Expected a full durable event payload")
        return value
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], dict):
        raise ValueError("Invalid panorama event frame")
    digest, body = value
    sweep = body.get("sweep")
    if (row["sequence"] < 1 or row["event_type"] != "sweep.received" or row["source_session"] is None
            or row["session_id"] != row["source_session"] or not isinstance(sweep, dict)
            or sweep.get("id") != reference or sweep.get("session_id") != row["source_session"]
            or sweep.get("stream_id") != row["source_stream"] or sweep.get("level_unit") != row["source_unit"]
            or "frequencies_hz" in sweep or "levels" in sweep
            or not isinstance(sweep.get("interval"), dict)
            or parse_time(sweep["interval"].get("started_at")) != parse_time(row["source_start"])
            or parse_time(sweep["interval"].get("ended_at")) != parse_time(row["source_end"])
            or _digest(row["source_hz"], row["source_levels"]) != digest):
        raise ValueError("Invalid panorama event reference or source")
    hz, levels = json.loads(row["source_hz"]), json.loads(row["source_levels"])
    if not isinstance(hz, list) or not hz or not isinstance(levels, list) or len(hz) != len(levels):
        raise ValueError("Invalid panorama vector dimensions")
    sweep["frequencies_hz"], sweep["levels"] = hz, levels
    return body


def verify(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if check_schema(db, version):
        cursor = db.cursor()
        cursor.row_factory = sqlite3.Row
        for row in cursor.execute(f"{JOINED} WHERE {REFERENCES}"):
            payload(row)


def materialize_copy(db):
    """Rebuild events only in a private snapshot under the caller's transaction."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not check_schema(db, version):
        return
    if db.execute("SELECT 1 FROM events WHERE sequence <= 0 LIMIT 1").fetchone():
        raise ValueError("Invalid durable event sequence")
    after = 0
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    query = f"{JOINED} WHERE e.sequence>? AND {REFERENCES} ORDER BY e.sequence LIMIT 1"
    while (row := cursor.execute(query, (after,)).fetchone()) is not None:
        after = row["sequence"]
        db.execute("UPDATE events SET payload_json=?,ori_sweep_ref_v1=NULL WHERE sequence=?",
                   (_json(payload(row)), after))
    db.execute("ALTER TABLE events DROP COLUMN ori_sweep_ref_v1")
    db.execute("PRAGMA user_version=6")


def _fresh(path):
    for suffix in ("", "-wal", "-shm", "-journal"):
        entry = Path(str(path) + suffix)
        if entry.exists() or entry.is_symlink():
            raise FileExistsError(entry)


def _publish(staged, destination):
    from .recovery_backup import _sync_directory

    staged.chmod(0o600)
    with staged.open("r+b") as file:
        os.fsync(file.fileno())
    _fresh(destination)
    os.link(staged, destination)
    _sync_directory(destination.parent)


def initialize(destination):
    from .session_store import SQLiteSessionStore

    destination = Path(destination).absolute()
    _fresh(destination)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".ori-compact-") as stage:
        path = Path(stage) / "archive.sqlite"
        with closing(SQLiteSessionStore(str(path), compact_sweep_events=True)):
            pass
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            verify(db)
        _publish(path, destination)


def materialize(source, destination):
    from .recovery_backup import _integrity

    destination = Path(destination).absolute()
    _fresh(destination)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".ori-materialize-") as stage:
        path = Path(stage) / "archive.sqlite"
        uri = Path(source).resolve(strict=True).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as original, closing(sqlite3.connect(path)) as db:
            original.execute("BEGIN")
            if original.execute("PRAGMA user_version").fetchone()[0] not in (6, VERSION):
                raise ValueError("Materialization requires archive schema 6 or 7")
            original.backup(db, pages=128)
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            _integrity(db)
            with db:
                db.execute("BEGIN IMMEDIATE")
                materialize_copy(db)
            _integrity(db)
        _publish(path, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init").add_argument("destination")
    convert = sub.add_parser("materialize")
    convert.add_argument("source")
    convert.add_argument("destination")
    args = parser.parse_args()
    if args.action == "init":
        initialize(args.destination)
    else:
        materialize(args.source, args.destination)


if __name__ == "__main__":
    main()
