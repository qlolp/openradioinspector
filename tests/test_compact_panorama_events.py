"""Full public panorama events and offline rollback across both durable formats."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from radioinspector import panorama_events
from radioinspector.contracts import MonitoringSession, SessionEvent, Sweep, parse_time, to_primitive
from radioinspector.monitoring.archive_backup import backup_archive
from radioinspector.recovery_backup import create_backup, restore_backup, verify_backup
from radioinspector.session_store import SQLiteSessionStore


FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "contracts/fixtures/measurement-v1.json")
                     .read_text(encoding="utf-8"))


def record(store, sequence=1):
    session = MonitoringSession.from_dict(FIXTURE["session"])
    store.save_session(session)
    sweep = Sweep.from_dict(FIXTURE["sweep"])
    item = to_primitive(sweep)
    item["interval"]["elapsed_ticks"] = 2**53 + 1
    item["vendor_field"] = {"$serde_json::private::Number": "kept verbatim"}
    event = SessionEvent(sequence, "sweep.received", parse_time("2026-11-11T02:03:04.654321Z"),
                         session.id, {"sweep": item, "source_note": "приём", "ori_sweep_ref_v1": "literal"})
    store.record_sweep(sweep, event=event)
    tail = SessionEvent(sequence + 1, "operator.note", event.occurred_at, session.id, {"message": "end"})
    store.append_event(tail)
    return [to_primitive(event), to_primitive(tail)]


class CompactPanoramaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "live.sqlite"

    def assert_events(self, path, expected, version):
        with closing(SQLiteSessionStore(str(path))) as store:
            self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], version)
            got = [to_primitive(item) for item in store.events(limit=100)]
            self.assertEqual(got, expected)
            for result in (store.next_stream_event(after=0), store.load_events(limit=2)[0]):
                self.assertEqual(to_primitive(result), expected[0])
                levels = result.payload["sweep"]["levels"]
                self.assertEqual([struct.pack(">d", v) for v in levels],
                                 [struct.pack(">d", v) for v in FIXTURE["sweep"]["levels"]])

    def test_legacy_default_survives_explicit_rejection_without_modifying_database(self):
        with closing(SQLiteSessionStore(str(self.path))) as store:
            expected = record(store)
            self.assertIsInstance(json.loads(store._connection.execute(
                "SELECT payload_json FROM events WHERE sequence=1").fetchone()[0]), dict)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            SQLiteSessionStore(str(self.path), compact_sweep_events=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assert_events(self.path, expected, 6)

    def test_wal_recovery_portable_and_materialized_copies(self):
        panorama_events.initialize(self.path)
        with closing(SQLiteSessionStore(str(self.path))) as store:
            expected = record(store)
            raw, reference = store._connection.execute(
                "SELECT payload_json,ori_sweep_ref_v1 FROM events WHERE sequence=1").fetchone()
            self.assertEqual(reference, FIXTURE["sweep"]["id"])
            self.assertNotIn("frequencies_hz", raw)
            self.assertNotIn('"levels"', raw)
            self.assertGreater(Path(str(self.path) + "-wal").stat().st_size, 0)
            bundle = self.root / "backup"
            create_backup(self.path, bundle)
            self.assertEqual(verify_backup(bundle)["archive_schema"], 7)
            restored, portable, rollback = (self.root / name for name in ("restore", "portable", "rollback"))
            restore_backup(bundle, restored)
            backup_archive(self.path, portable)
            panorama_events.materialize(self.path, rollback)
        for path, version in ((self.path, 7), (restored, 7), (portable, 6), (rollback, 6)):
            self.assert_events(path, expected, version)

    def test_inconsistent_vector_rolls_back_the_whole_ingestion(self):
        with closing(SQLiteSessionStore(str(self.path), compact_sweep_events=True)) as store:
            expected = record(store)
            second = Sweep.from_dict(dict(FIXTURE["sweep"], id="second"))
            body = to_primitive(second)
            body["levels"][1] = 0.0
            event = SessionEvent(3, "sweep.received", second.interval.ended_at,
                                 second.session_id, {"sweep": body})
            with self.assertRaisesRegex(ValueError, "disagrees"):
                store.record_sweep(second, event=event)
            for table in ("sweeps", "sweep_ingestion", "monitoring_sweep_index"):
                self.assertEqual(store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)
            self.assertEqual([to_primitive(item) for item in store.events()], expected)

    def test_no_overwrite_sidecars_bad_markers_and_corrupt_references(self):
        with closing(SQLiteSessionStore(str(self.path), compact_sweep_events=True)) as store:
            record(store)
        for destination in [self.path, self.root / "other"]:
            stale = Path(str(destination) + "-journal")
            if destination != self.path:
                stale.write_bytes(b"existing journal")
            with self.assertRaises(FileExistsError):
                panorama_events.materialize(self.path, destination)
            with self.assertRaises(FileExistsError):
                panorama_events.initialize(destination)
        bad = self.root / "invalid"
        with closing(sqlite3.connect(self.path)) as db:
            corruptions = (
                "UPDATE events SET ori_sweep_ref_v1='dangling' WHERE sequence=1",
                "UPDATE events SET ori_sweep_ref_v1=NULL WHERE sequence=1",
                "UPDATE events SET ori_sweep_ref_v1=NULL, payload_json=char(32,9,10,13)||payload_json WHERE sequence=1",
                "UPDATE sweeps SET levels_json='[0,0,0,0]'",
                "UPDATE events SET event_type='other' WHERE sequence=1",
            )
            for index, sql in enumerate(corruptions):
                with self.subTest(sql=sql), closing(sqlite3.connect(bad)) as change:
                    db.backup(change)
                    change.execute(sql)
                    change.commit()
                    with closing(SQLiteSessionStore(str(bad))) as store:
                        with self.assertRaises(ValueError):
                            store.events()
                    backup = self.root / f"rejected-backup-{index}"
                    with self.assertRaises(ValueError):
                        create_backup(bad, backup)
                    self.assertFalse(backup.exists())
                    destination = self.root / f"rejected-{index}"
                    with self.assertRaises(ValueError):
                        panorama_events.materialize(bad, destination)
                    self.assertFalse(destination.exists())
            db.execute("PRAGMA user_version=6")
        with self.assertRaises(ValueError):
            SQLiteSessionStore(str(self.path))
