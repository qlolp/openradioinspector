"""Compare v6/v7 growth on identical synthetic sweeps using the canonical SQLite writer."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import partial
import json
from pathlib import Path
import random
import subprocess
import tempfile
import time

from radioinspector.contracts import MonitoringSession, SessionEvent, Sweep, TimeInterval, to_primitive
from radioinspector.session_store import SQLiteSessionStore


def _session():
    return MonitoringSession.from_dict({"id": "storage-session", "name": "Synthetic spectrum",
        "created_at": "2026-01-01T00:00:00.000000Z",
        "streams": [{"id": "spectrum", "role": "spectrum_analyzer", "device": "synthetic",
                     "level_unit": "dBm"}]})


def _batches(session, sweeps, points, batch):
    rng = random.Random(42)
    hz = tuple(960_000_000 + i * 10_000 for i in range(points))
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for first in range(0, sweeps, batch):
        items = []
        for index in range(first, min(first + batch, sweeps)):
            at = stamp + timedelta(seconds=index)
            item = Sweep(session.id, "spectrum", TimeInterval(at, at + timedelta(milliseconds=900)),
                         hz, tuple(round(rng.uniform(-110, -40), 4) for _ in hz), "dBm",
                         id=f"source-{index:08d}", settings={"rbw_hz": 10000})
            items.append(item)
        yield items


def measure(path, *, compact, sweeps, points, batch):
    session = _session()
    with closing(SQLiteSessionStore(str(path), compact_sweep_events=compact)) as store:
        store.save_session(session)
        db = store._connection
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        baseline = store.archive_capacity()["database_bytes"]
        started = time.monotonic()
        sequence = 0
        for items in _batches(session, sweeps, points, batch):
            events = [SessionEvent(sequence + offset, "sweep.received", item.interval.started_at,
                                   session.id, {"sweep": to_primitive(item)})
                      for offset, item in enumerate(items, start=1)]
            sequence += len(items)
            store.record_sweeps(items, events=events)
        elapsed = time.monotonic() - started
        wal = Path(str(path) + "-wal")
        before = {"main_bytes": path.stat().st_size, "wal_bytes": wal.stat().st_size}
        growth = store.archive_capacity()["database_bytes"] - baseline
        ledger = db.execute("SELECT sum(length(CAST(payload_json AS BLOB))) FROM events").fetchone()[0]
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"schema": db.execute("PRAGMA user_version").fetchone()[0],
                "logical_growth_bytes": growth, "logical_growth_bytes_per_sweep": growth / sweeps,
                "event_payload_bytes": ledger, "before_checkpoint": before,
                "after_checkpoint": {"main_bytes": path.stat().st_size, "wal_bytes": wal.stat().st_size},
                "ingest_seconds": round(elapsed, 4)}


def native_measure(program, path, *, compact, sweeps, points, batch):
    session = _session()
    with subprocess.Popen([str(program), str(path), "7" if compact else "6"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8") as process:
        try:
            process.stdin.write(json.dumps(to_primitive(session)) + "\n")
            for items in _batches(session, sweeps, points, batch):
                process.stdin.write(json.dumps(to_primitive(items), separators=(",", ":")) + "\n")
            process.stdin.close()
            result = process.stdout.read()
        finally:
            process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Native archive benchmark writer failed")
    return json.loads(result)


def compare(sweeps=128, points=1024, batch=8, native=None):
    if not (sweeps > 0 and points > 0 and batch > 0):
        raise ValueError("Sweep count, points and batch size must be positive")
    with tempfile.TemporaryDirectory(prefix="ori-panorama-growth-") as directory:
        writer = partial(native_measure, native) if native else measure
        full, compact = (writer(Path(directory) / f"{version}.sqlite", compact=(version == 7),
                                 sweeps=sweeps, points=points, batch=batch) for version in (6, 7))
    return {"writer": "rust" if native else "python", "workload": {"sweeps": sweeps, "points_per_sweep": points, "batch": batch, "seed": 42},
            "full": full, "compact": compact,
            "compact_to_full_growth": compact["logical_growth_bytes"] / full["logical_growth_bytes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweeps", type=int, default=128)
    parser.add_argument("--points", type=int, default=1024)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--native", type=Path, help="Built ori-archive panorama_storage example")
    args = parser.parse_args()
    print(json.dumps(compare(args.sweeps, args.points, args.batch, args.native), indent=2))


if __name__ == "__main__":
    main()
