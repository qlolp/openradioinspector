"""Synthetic ingestion-order detector coverage; no workers or real archives."""

import copy
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from radioinspector.automatic_detection import AutomaticCandidateDetector
from radioinspector.contracts import Sweep, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import (
    ArchiveCapacityExceededError,
    SQLiteSessionStore,
)
from tests.test_automatic_detection import spectral_sweep
from tests.test_device_workers import simulated_session_payload


@contextmanager
def fixture(sqlite=False):
    store = SQLiteSessionStore(":memory:") if sqlite else None
    service = SessionService(store=store, history_limit=2)
    payload = simulated_session_payload()
    payload["streams"][1] = copy.deepcopy(payload["streams"][0])
    payload["streams"][1]["port"] = "sim://other"
    session = service.create_session(payload)
    service.start_session(session.id)
    detector = AutomaticCandidateDetector(service, start_background=False)
    try:
        yield service, store, session, detector
    finally:
        detector.close()
        service.close()


def sweep(session, number, *, stream=0, signal=False, timestamp=None):
    payload = spectral_sweep(
        session.id, session.streams[stream].id, f"sweep-{number:05}",
        number if timestamp is None else timestamp,
    )
    payload["levels"] = [-90.0] * 7
    if signal:
        payload["levels"][2] = -40.0
    return payload


class DetectionCursorTests(unittest.TestCase):
    def test_backlog_and_unrelated_stream_flood(self):
        for sqlite in (False, True):
            for mixed in (False, True):
                with self.subTest(sqlite=sqlite, mixed=mixed), fixture(sqlite) as f:
                    service, _, session, detector = f
                    detector.configure(session.id, {
                        "enabled": True, "stream_id": session.streams[0].id,
                    })
                    for n in range(1001):
                        service.ingest_sweep(sweep(session, n, signal=n == 0,
                                                   stream=1 if mixed and n else 0))
                    self.assertEqual(len(detector.run_once(session.id)), 1)
                    self.assertEqual(detector.run_once(session.id), [])
                    self.assertEqual(detector.run_once(session.id), [])
                    expected = 1 if mixed else 1001
                    self.assertEqual(detector.status(session.id)["runtime"]["sweeps_processed"], expected)
                    self.assertEqual(len(service.list_sweeps(session.id, limit=None)), 1001)
                    self.assertEqual(len(service.list_candidates(session.id)), 1)

    def test_pages_backdating_opt_in_switch_and_restart(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite), fixture(sqlite) as f:
                service, _, session, detector = f
                service.ingest_sweep(sweep(session, 0, signal=True, timestamp=9000))
                detector.configure(session.id, {"enabled": True, "stream_id": session.streams[0].id})
                self.assertEqual(detector.run_once(session.id), [])
                for n in range(1, 2003):
                    service.ingest_sweep(sweep(session, n, timestamp=1))
                self.assertEqual(detector.run_once(session.id), [])
                self.assertEqual(detector.status(session.id)["runtime"]["sweeps_processed"], 1000)
                detector.run_once(session.id)
                detector.run_once(session.id)
                self.assertEqual(detector.status(session.id)["runtime"]["sweeps_processed"], 2002)
                service.ingest_sweep(sweep(session, 2003, stream=1, signal=True, timestamp=0))
                detector.configure(session.id, {"stream_id": session.streams[1].id})
                self.assertEqual(len(detector.run_once(session.id)), 1)
                detector.configure(session.id, {"enabled": False})
                service.ingest_sweep(sweep(session, 2004, signal=True))
                detector.configure(session.id, {"enabled": True, "stream_id": None})
                self.assertEqual(detector.run_once(session.id), [])
                replacement = AutomaticCandidateDetector(service, start_background=False)
                try:
                    self.assertFalse(replacement.status(session.id)["policy"]["enabled"])
                    replacement.configure(session.id, {"enabled": True})
                    self.assertEqual(replacement.run_once(session.id), [])
                finally:
                    replacement.close()
                arrival = service.sweeps_after_ingestion(session.id, after=0, limit=3)
                self.assertEqual([x.id for _, x in arrival], [f"sweep-{n:05}" for n in range(3)])
                public = service.list_sweeps(session.id, limit=None)
                self.assertEqual(public[-1].id, "sweep-00000")
                self.assertEqual(public[0].id, "sweep-02003")

    def test_empty_selected_stream_switch_and_close_admitted_page(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite), fixture(sqlite) as f:
                service, _, session, detector = f
                service.ingest_sweep(sweep(session, 0, stream=1, signal=True))
                detector.configure(session.id, {"enabled": True, "stream_id": session.streams[0].id})
                service.ingest_sweep(sweep(session, 1, stream=1, signal=True))
                self.assertEqual(detector.run_once(session.id), [])
                self.assertEqual(detector._cursor[session.id], 1)
                detector.configure(session.id, {"stream_id": session.streams[1].id})
                original = service.sweeps_after_ingestion

                def close_after_admission(*args, original=original, detector=detector, **kwargs):
                    page = original(*args, **kwargs)
                    detector.close()
                    return page

                with patch.object(service, "sweeps_after_ingestion", close_after_admission):
                    created = detector.run_once(session.id)
                self.assertEqual(len(created), 1)
                self.assertEqual(created[0].interval.started_at,
                                 Sweep.from_dict(sweep(session, 1)).interval.started_at)
                self.assertEqual(detector.run_once(session.id), [])

    def test_failure_retries_record_not_prior_page_progress(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite), fixture(sqlite) as f:
                service, _, session, detector = f
                detector.configure(session.id, {"enabled": True})
                for n in range(3):
                    service.ingest_sweep(sweep(session, n, signal=n == 1))
                original = service.create_candidate
                with patch.object(service, "create_candidate", side_effect=RuntimeError("synthetic")), self.assertRaisesRegex(RuntimeError, "synthetic"):
                    detector.run_once(session.id)
                self.assertEqual(detector._cursor[session.id], 1)
                self.assertEqual(len(detector.run_once(session.id)), 1)
                self.assertEqual(detector._cursor[session.id], 3)
                self.assertEqual(service.create_candidate, original)

    def test_provenance_publish_failure_is_not_exactly_once(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite), fixture(sqlite) as f:
                service, _, session, detector = f
                detector.configure(session.id, {"enabled": True})
                service.ingest_sweep(sweep(session, 0, signal=True))
                with patch.object(service, "publish", side_effect=RuntimeError("publish failed")), self.assertRaisesRegex(RuntimeError, "publish failed"):
                    detector.run_once(session.id)
                self.assertEqual(detector._cursor[session.id], 0)
                self.assertEqual(len(service.list_candidates(session.id)), 1)
                self.assertEqual(detector.run_once(session.id), [])
                self.assertEqual(len(service.list_candidates(session.id)), 1)
                self.assertEqual(detector._cursor[session.id], 1)
                # Existing protected-candidate dedup avoids a second candidate,
                # but retry does not reconstruct the failed provenance event.
                events = service.events(after=0, limit=100)
                self.assertFalse(any(e.event_type == "automatic_detection.candidate_created" for e in events))

    def test_configure_waits_for_dispatch_without_holding_global_lock(self):
        with fixture() as f:
            service, _, session, detector = f
            detector.configure(session.id, {"enabled": True})
            service.ingest_sweep(sweep(session, 0, signal=True))
            entered, release, disabled = threading.Event(), threading.Event(), threading.Event()
            attempted = threading.Event()
            underlying = detector._dispatch_locks[session.id]

            class DispatchProbe:
                def __enter__(self):
                    if threading.current_thread().name == "cursor-test-configure":
                        attempted.set()
                    underlying.acquire()
                    return self

                def __exit__(self, *args):
                    underlying.release()

            detector._dispatch_locks[session.id] = DispatchProbe()
            original = service.sweeps_after_ingestion
            errors = []

            def blocked(*args, **kwargs):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test release timeout")
                return original(*args, **kwargs)

            def run(action):
                try:
                    action()
                except Exception as exc:  # noqa: BLE001 - propagate worker failures to assertion
                    errors.append(exc)

            def disable():
                detector.configure(session.id, {"enabled": False})
                disabled.set()

            with patch.object(service, "sweeps_after_ingestion", blocked):
                worker = threading.Thread(target=run, args=(lambda: detector.run_once(session.id),))
                config = threading.Thread(target=run, args=(disable,), name="cursor-test-configure")
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    config.start()
                    self.assertTrue(attempted.wait(2))
                    self.assertFalse(disabled.is_set())
                    self.assertTrue(detector._lock.acquire(timeout=1))
                    detector._lock.release()
                finally:
                    release.set()
                    worker.join(3)
                    if config.ident is not None:
                        config.join(3)
                self.assertFalse(worker.is_alive())
                self.assertFalse(config.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(disabled.is_set())
                self.assertEqual(detector.run_once(session.id), [])

    def test_boundary_read_does_not_hold_detector_global_lock(self):
        with fixture() as f:
            service, _, session, detector = f
            original = service.sweep_ingestion_boundary

            def boundary(*args):
                result = []
                def check():
                    acquired = detector._lock.acquire(timeout=0.5)
                    result.append(acquired)
                    if acquired:
                        detector._lock.release()
                thread = threading.Thread(target=check)
                thread.start()
                thread.join(1)
                self.assertEqual(result, [True])
                return original(*args)

            with patch.object(service, "sweep_ingestion_boundary", boundary):
                detector.configure(session.id, {"enabled": True})


class IngestionLedgerTests(unittest.TestCase):
    def test_single_batch_eventless_and_rollback(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite), fixture(sqlite) as f:
                service, store, session, _ = f
                service.ingest_sweep(sweep(session, 0))
                service.ingest_sweeps([sweep(session, 1), sweep(session, 2)])
                with self.assertRaises(ValueError):
                    service.ingest_sweeps([sweep(session, 3), sweep(session, 0)])
                self.assertEqual(service.sweep_ingestion_boundary(session.id), 3)
                self.assertEqual(len(service.sweeps_after_ingestion(session.id, after=0)), 3)
                if store:
                    store.record_sweep(Sweep.from_dict(sweep(session, 4)))
                    self.assertEqual(service.sweep_ingestion_boundary(session.id), 4)
                    with patch.object(store, "_insert_event", side_effect=RuntimeError("rollback")), self.assertRaisesRegex(RuntimeError, "rollback"):
                        service.ingest_sweeps([sweep(session, 5), sweep(session, 6)])
                    self.assertEqual(service.sweep_ingestion_boundary(session.id), 4)
                    self.assertEqual(len(service.list_sweeps(session.id, limit=None)), 4)
                    with self.assertRaises(sqlite3.IntegrityError), store._connection:
                        store._connection.execute(
                            "INSERT INTO sweep_ingestion(sweep_id,session_id,stream_id) VALUES(?,?,?)",
                            ("missing", session.id, session.streams[0].id),
                        )

    def test_capacity_post_write_rolls_back_ledger(self):
        with fixture(True) as f:
            service, store, session, _ = f
            original = store._check_capacity_locked

            def reject(*, before_write):
                if not before_write:
                    raise ArchiveCapacityExceededError(100, 50)
                return original(before_write=before_write)

            with patch.object(store, "_check_capacity_locked", reject), self.assertRaises(ArchiveCapacityExceededError):
                service.ingest_sweep(sweep(session, 0))
            self.assertEqual(service.sweep_ingestion_boundary(session.id), 0)
            self.assertEqual(service.list_sweeps(session.id), [])
            service.ingest_sweep(sweep(session, 0))
            self.assertEqual(service.sweep_ingestion_boundary(session.id), 1)

    def test_populated_v5_migration_preserves_raw_rows_rollback_and_reopen(self):
        with tempfile.TemporaryDirectory(prefix="ori-cursor-test-") as tmp:
            path = str(Path(tmp) / "synthetic.sqlite")
            store = SQLiteSessionStore(path)
            service = SessionService(store=store)
            session = service.create_session(simulated_session_payload())
            for n in (2, 0, 1):
                service.ingest_sweep(sweep(session, n, timestamp=3 - n))
            service.create_sweep_reference(session.id, {"name": "preserved", "sweep_id": "sweep-00000"})
            service.create_candidate(session.id, {
                "id": "preserved-candidate", "source_stream_id": session.streams[0].id,
                "interval": sweep(session, 0)["interval"],
                "center_frequency_hz": 100_200_000, "level_unit": "dBm",
            })
            service.enqueue_investigation(session.id, "preserved-candidate", {
                "id": "preserved-investigation", "target_stream_id": session.streams[1].id,
            })
            with store._connection:
                store._connection.execute(
                    "UPDATE investigations SET listening_started_at = ? WHERE id = ?",
                    ("2026-01-01T00:00:00.000000Z", "preserved-investigation"),
                )
            from tests.test_measurement_results import payload
            service.save_measurement_result(session.id, payload(stream_id=session.streams[0].id))
            preservation_queries = (
                "SELECT id, methodology, result_code, values_json FROM measurement_results ORDER BY id",
                "SELECT id, session_id, stream_id, frequency_hz, started_at, ended_at, created_at FROM measurement_results ORDER BY id",
                "SELECT name, sql FROM sqlite_master WHERE name IN ('measurement_results_session_time', 'measurement_results_methodology', 'sweeps_reference_owner', 'sweep_references_session_time') ORDER BY name",
                "SELECT id, listening_started_at FROM investigations ORDER BY id",
                "SELECT id, state, center_frequency_hz FROM candidates ORDER BY id",
                "SELECT id, sweep_id, stream_id FROM sweep_references ORDER BY id",
                "SELECT sequence, payload_json FROM events ORDER BY sequence",
            )
            preserved = [list(map(tuple, store._connection.execute(q))) for q in preservation_queries]
            expected = [to_primitive(x) for x in service.list_sweeps(session.id, limit=None)]
            raw_before = [tuple(row) for row in store._connection.execute("SELECT id, levels_json FROM sweeps ORDER BY id")]
            with store._connection:
                store._connection.execute("DROP TABLE sweep_ingestion")
                store._connection.execute("PRAGMA user_version = 5")
            service.close()
            original = SQLiteSessionStore._create_ingestion_schema

            def fail_after_backfill(instance, **kwargs):
                original(instance, **kwargs)
                raise RuntimeError("migration failure")

            with patch.object(SQLiteSessionStore, "_create_ingestion_schema", fail_after_backfill), self.assertRaisesRegex(RuntimeError, "migration failure"):
                SQLiteSessionStore(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
                self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='sweep_ingestion'").fetchone())
                self.assertEqual(connection.execute("SELECT id, levels_json FROM sweeps ORDER BY id").fetchall(), raw_before)
                self.assertEqual([list(map(tuple, connection.execute(q))) for q in preservation_queries], preserved)
            for iteration in range(2):
                store = SQLiteSessionStore(path)
                self.assertEqual(
                    [list(map(tuple, store._connection.execute(q))) for q in preservation_queries], preserved,
                )
                service = SessionService(store=store)
                try:
                    self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], 6)
                    self.assertEqual(service.sweep_ingestion_boundary(session.id), 3)
                    self.assertEqual([to_primitive(x) for x in service.list_sweeps(session.id, limit=None)], expected)
                    self.assertEqual([s.id for _, s in service.sweeps_after_ingestion(session.id, after=0)],
                                     [f"sweep-{n:05}" for n in range(3)])
                    if iteration:
                        service.ingest_sweep(sweep(session, 3, timestamp=0))
                        self.assertEqual(service.sweep_ingestion_boundary(session.id), 4)
                finally:
                    service.close()

    def test_query_validation(self):
        with fixture() as f:
            service, _, session, _ = f
            for kwargs in ({"after": True}, {"after": -1}, {"after": 0, "limit": 1001},
                           {"after": 0, "limit": False}):
                with self.assertRaises(ValueError):
                    service.sweeps_after_ingestion(session.id, **kwargs)


class CursorSixMigrationTests(unittest.TestCase):
    def test_direct_and_staged_upgrade_preserve_current_main_data(self):
        from tests.test_listening_duration import ListeningMigrationTests
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "synthetic-v4.sqlite"
                legacy = ListeningMigrationTests()
                legacy.legacy(path)
                with sqlite3.connect(path) as connection:
                    before = legacy.archive_fixture_values(connection)
                if staged:
                    # Exercise the actual v5 listening migration without the v6
                    # additive ledger, then reopen with the complete v6 code.
                    with patch("radioinspector.session_store.SCHEMA_VERSION", 5), patch.object(
                        SQLiteSessionStore, "_create_ingestion_schema"
                    ), SQLiteSessionStore(str(path)) as store:
                        self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], 5)
                        with store._connection:
                            store._connection.execute(
                                "UPDATE investigations SET listening_started_at = ? WHERE id = ?",
                                ("2026-01-01T00:00:00.000000Z", "investigation-active"),
                            )
                for reopen in range(2):
                    with SQLiteSessionStore(str(path)) as store:
                        self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], 6)
                        self.assertEqual(legacy.archive_fixture_values(store._connection), before)
                        timestamp = store._connection.execute(
                            "SELECT listening_started_at FROM investigations WHERE id = 'investigation-active'"
                        ).fetchone()[0]
                        self.assertEqual(timestamp, "2026-01-01T00:00:00.000000Z" if staged else None)
                        rows = store._connection.execute(
                            "SELECT sequence, sweep_id FROM sweep_ingestion ORDER BY sequence"
                        ).fetchall()
                        self.assertEqual([tuple(r) for r in rows], [(1, "sweep-0")])
                        self.assertEqual(store._connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_direct_upgrade_rolls_back_both_stages(self):
        from tests.test_listening_duration import ListeningMigrationTests
        for stage in ("_create_reference_schema", "_create_ingestion_schema"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "synthetic-rollback.sqlite"
                legacy = ListeningMigrationTests()
                legacy.legacy(path)
                with sqlite3.connect(path) as connection:
                    before = legacy.archive_fixture_values(connection)
                original = getattr(SQLiteSessionStore, stage)

                def fail(store, *args, _original=original, **kwargs):
                    _original(store, *args, **kwargs)
                    raise RuntimeError("synthetic stage failure")

                with patch.object(SQLiteSessionStore, stage, fail), self.assertRaisesRegex(RuntimeError, "stage failure"):
                    SQLiteSessionStore(str(path))
                with sqlite3.connect(path) as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                    self.assertEqual(legacy.archive_fixture_values(connection), before)
                    self.assertNotIn("listening_started_at", [r[1] for r in connection.execute("PRAGMA table_info(investigations)")])
                    self.assertIsNone(connection.execute(
                        "SELECT name FROM sqlite_master WHERE name = 'sweep_ingestion'"
                    ).fetchone())
