"""Owned SQLite/oracle fixtures from actual Python guard selection operations.

Run from the repository root after benchmark quiet periods. No hardware,
production measurements, paths supplied by RPC, or external service is used.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from radioinspector.contracts import MonitoringSession, Sweep, format_time, parse_time
from radioinspector.monitoring.baselines import BaselineOperations
from radioinspector.monitoring.preparation import PreparationOperations
from radioinspector.monitoring.store import ProjectStore
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore, _archive_time

HERE = Path(__file__).resolve().parent / "fixtures"
STAMP = "2026-11-11T00:00:00.123457Z"


class Harness(PreparationOperations):
    pass


def position(row):
    return {"id": row["id"], "rowid": row["capture_order"], "session_id": row["session_id"],
            "stream_id": row["stream_id"], "started_at": _archive_time(parse_time(row["started_at"]))}


def make_case(store, name, count):
    sid, stream = "selection/" + name, "analyzer/" + name
    session = MonitoringSession.from_dict({"id": sid, "name": name, "created_at": STAMP,
        "streams": [{"id": stream, "device": "owned synthetic source", "role": "spectrum_analyzer",
                     "level_unit": "dBm"}]})
    store.save_session(session)
    base = datetime(2026, 1, 1, 0, 0, 0, 123457, tzinfo=timezone.utc)
    if name == "long":
        base = base.replace(year=1900)
    holes = []
    for i in range(count):
        if name == "flat":
            instant = base
        elif name == "long":
            instant = base + timedelta(days=5500 * i, microseconds=i * i + 1)
        else:
            instant = base + timedelta(microseconds=i * 100003 + (i % 7))
        identity = f"source-{name}-{count-i:05d}"
        if name in ("high", "few") and i in (10, 11, 12):
            instant = base + timedelta(microseconds=10 * 100003 + 3)
            identity = ("tie-\ue000", "tie-\U00010000", "tie-😀")[i - 10] + "/" + name
        source = {"id": identity, "session_id": sid, "stream_id": stream,
            "interval": {"started_at": format_time(instant), "ended_at": format_time(instant + timedelta(microseconds=10001))},
            "frequencies_hz": [100, 200], "levels": [-0.0, -90.00000000000001],
            "level_unit": "dBm", "settings": {"rbw_hz": 10000, "sequence": i}}
        store.record_sweep(Sweep.from_dict(source))
        if name == "high" and i % 47 == 5 and i < 700:
            holes.append(identity)
        if name == "few" and i == 0:
            # list_sweeps' original raw-time text ordering is part of authority.
            offset = instant.astimezone(timezone(timedelta(hours=3))).isoformat()
            with store._connection:
                store._connection.execute("UPDATE sweeps SET started_at=? WHERE id=?", (offset, identity))
    # Delete only after later captures exist; deleting the current maximum ROWID
    # would let SQLite reuse it on the next insert instead of retaining a hole.
    with store._connection:
        for identity in holes:
            store._connection.execute("DELETE FROM monitoring_sweep_index WHERE sweep_id=?", (identity,))
            store._connection.execute("DELETE FROM sweep_ingestion WHERE sweep_id=?", (identity,))
            store._connection.execute("DELETE FROM sweeps WHERE id=?", (identity,))
    return {"active_session_id": sid, "analyzer_stream_id": stream}


def make_events(store, high_session):
    records = ProjectStore(store)
    records.put("project", {"id": "guard-project", "created_at": STAMP, "session_ids": [high_session]})
    for i in range(1005):
        records.put("event", {"id": f"event-{i:04d}", "project_id": "guard-project", "created_at": STAMP,
            "state": "open", "run_session_id": high_session, "baseline_id": "baseline-A",
            "kind": "broadband_change", "disposition": "unreviewed", "last_seen": STAMP,
            "source_refs": [{"session_id": high_session, "stream_id": "analyzer/high", "sweep_id": "evidence"}]})
    common = {"project_id": "guard-project", "created_at": STAMP, "run_session_id": high_session,
        "state": "closed", "baseline_id": "baseline-A", "kind": "new_signal", "disposition": "unreviewed",
        "last_seen": "2026-11-11T00:00:01.123457Z",
        "source_refs": [{"session_id": high_session, "stream_id": "analyzer/high", "sweep_id": "evidence"}]}
    cases = [dict(common, id="pending-original"),
        dict(common, id="pending-eligible-𐀀", analysis={"state": None}),
        dict(common, id="pending-eligible-\ue000", analysis={"state": None}),
        dict(common, id="newer-excluded-dense", kind="dense_change", last_seen="2026-11-11T00:00:02Z"),
        dict(common, id="newer-excluded-authorized", disposition="authorized", last_seen="2026-11-11T00:00:03Z"),
        dict(common, id="newer-excluded-queued", analysis={"state": "queued"}, last_seen="2026-11-11T00:00:04Z"),
        dict(common, id="foreign-session", source_refs=[{"session_id": "foreign"}], last_seen="2026-11-11T00:00:05Z"),
        dict(common, id="foreign-project", project_id="foreign-project", last_seen="2026-11-11T00:00:06Z"),
        dict(common, id="other-baseline", baseline_id="baseline-B", state="open", kind="broadband_change"),
        dict(common, id="other-run", run_session_id="other", state="open", kind="broadband_change")]
    for event in cases:
        records.put("event", event)
    for i in range(130):
        records.put("event-source", {"id": f"evidence-{i:04d}", "project_id": "guard-project", "created_at": STAMP,
            "event_id": "event-0000", "session_id": high_session, "stream_id": "analyzer/high", "sweep_id": f"source-{i}"})
    records.put("event-source", {"id": "foreign-evidence", "project_id": "foreign-project", "created_at": STAMP,
        "event_id": "event-0000", "session_id": "foreign", "stream_id": "foreign", "sweep_id": "foreign"})
    return {"open_ids": [x["id"] for x in records.open_events("guard-project", high_session, "baseline-A")],
            "pending_ids": [x["id"] for x in records.pending_receiver_events("guard-project", high_session)],
            "event_source_ids": [f"evidence-{i:04d}" for i in range(130)]}


def generate(encoding):
    target = HERE / f"guard_selection_{encoding.lower()}.sqlite"
    target.unlink(missing_ok=True)
    with sqlite3.connect(target) as raw:
        raw.execute(f"PRAGMA encoding='{encoding}'")
        raw.execute("CREATE TABLE owned_fixture_marker(value TEXT)")
        raw.execute("INSERT INTO owned_fixture_marker VALUES ('synthetic guard query parity')")
    store = SQLiteSessionStore(str(target))
    projects = [make_case(store, name, count) for name, count in
                (("empty", 0), ("few", 19), ("flat", 25), ("high", 750), ("long", 30))]
    harness = Harness()
    harness.sessions = SessionService(store=store)
    expected = {"cases": []}
    for project in projects:
        sweeps, period = harness.baseline_samples(project)
        collected = harness.baseline_collection(project)
        sid, stream = project["active_session_id"], project["analyzer_stream_id"]
        bounds = store._connection.execute("""SELECT MIN(i.started_at),MAX(i.ended_at)
            FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id WHERE i.session_id=? AND s.stream_id=?""",
            (sid, stream)).fetchone()
        pending = BaselineOperations._pending_sweeps(harness, project, limit=64)
        expected["cases"].append({"session_id": sid, "stream_id": stream,
            "selection": {"sweep_ids": [s.id for s in sweeps], "count": collected["sweeps"],
                "first_started_at": bounds[0], "last_ended_at": bounds[1],
                "elapsed_seconds": collected["elapsed_seconds"], "sweep_period_seconds": period},
            "pending": [{"id": s.id, "rowid": rowid, "session_id": s.session_id, "stream_id": s.stream_id,
                         "started_at": _archive_time(s.interval.started_at)} for rowid, s in pending]})
    expected["events"] = make_events(store, "selection/high")
    positions = store._connection.execute("""SELECT rowid AS capture_order,id,session_id,stream_id,started_at
        FROM sweeps ORDER BY rowid LIMIT 256""").fetchall()
    expected["positions"] = {row["id"]: position(row) for row in positions}
    expected["position_ids"] = [row["id"] for row in positions[:254]] + ["absent", positions[0]["id"]]
    expected["positions"] = {key: value for key, value in expected["positions"].items() if key in expected["position_ids"]}
    with store._connection:
        store._connection.execute("DROP TABLE sweep_ingestion")
        store._connection.execute("PRAGMA user_version=5")
    store._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store._connection.execute("PRAGMA journal_mode=DELETE")
    store.close()
    return expected


def main():
    HERE.mkdir(exist_ok=True)
    oracle = {encoding: generate(encoding) for encoding in ("UTF-8", "UTF-16")}
    (HERE / "guard_selection_oracle.json").write_text(json.dumps(oracle, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    rounding = []
    for micros in (-315537897599999999, -9007199254740993, -60000003, 0, 1, 235,
                   1000001, 60000003, 9007199254740993, 9007199254741111, 315537897599999999):
        duration = timedelta(microseconds=micros)
        for index in range(236):
            result = duration * (index / 235)
            rounded = ((result.days * 86400 + result.seconds) * 1000000 + result.microseconds)
            rounding.append({"micros": micros, "index": index, "rounded": rounded,
                             "seconds": duration.total_seconds()})
    (HERE / "guard_selection_rounding.json").write_text(json.dumps(rounding, allow_nan=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
