"""Synthetic listening-origin regressions; no device or user archive access."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from radioinspector.contracts import (
    Investigation,
    InvestigationState,
    TimeInterval,
    to_primitive,
)
from radioinspector.session_service import SessionService
from radioinspector.session_store import SCHEMA_VERSION, SQLiteSessionStore
from tests.test_automatic_completion import CompletionSetup
from tests.test_persistence import observation_payload
from tests.test_platform import BASE


class ListeningDurationTests(CompletionSetup, unittest.TestCase):
    def setUp(self):
        self.setup_scheduler()

    def tearDown(self):
        self.close()

    def slow_active(self):
        def tune(*args):
            self.now += timedelta(seconds=3)
            return {}
        self.tuned = tune
        investigation = self.active()
        self.scheduler.configure(self.session.id, {
            "enabled": True, "minimum_listening_seconds": 2.0,
        })
        payload = observation_payload(self.session.id, self.receiver_id, 0)
        payload["frequency_hz"] = investigation.target_frequency_hz
        payload["interval"] = to_primitive(TimeInterval(self.now, self.now + timedelta(milliseconds=100)))
        self.now += timedelta(milliseconds=100)
        self.service.ingest_observation(payload)
        return investigation

    def test_slow_tune_and_exact_boundary(self):
        investigation = self.slow_active()
        self.assertEqual(investigation.started_at, BASE)
        self.assertEqual(investigation.listening_started_at, BASE + timedelta(seconds=3))
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.now = BASE + timedelta(seconds=4, milliseconds=999)
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.now = BASE + timedelta(seconds=5)
        self.assertEqual(self.scheduler.run_once(self.session.id).state, InvestigationState.COMPLETED)

    def test_decoding_retains_origin(self):
        investigation = self.slow_active()
        # No public decoding transition currently exists; exercise its active-state contract.
        self.service._investigations[investigation.id].state = InvestigationState.DECODING
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.now = BASE + timedelta(seconds=5)
        result = self.scheduler.run_once(self.session.id)
        self.assertEqual(result.listening_started_at, investigation.listening_started_at)

    def test_legacy_positive_minimum_closed_zero_minimum_allowed(self):
        investigation = self.slow_active()
        self.service._investigations[investigation.id].listening_started_at = None
        self.now += timedelta(days=1)
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.scheduler.configure(self.session.id, {"minimum_listening_seconds": 0})
        self.assertEqual(self.scheduler.run_once(self.session.id).state, InvestigationState.COMPLETED)

    def test_failed_tune_has_no_origin(self):
        def tune(*args):
            self.now += timedelta(seconds=3)
            raise RuntimeError("synthetic failure")
        self.tuned = tune
        result = self.active()
        self.assertEqual(result.state, InvestigationState.FAILED)
        self.assertIsNone(result.listening_started_at)
        self.assertEqual(result.started_at, BASE)

    def test_cancel_during_tune_has_no_origin(self):
        def tune(*args):
            self.service.cancel_investigation(self.session.id, "investigation-active")
            return {}
        self.tuned = tune
        result = self.active()
        self.assertEqual(result.state, InvestigationState.CANCELLED)
        self.assertIsNone(result.listening_started_at)

    def test_http_and_windows_decode_additive_field(self):
        import threading

        from radioinspector.web.server import create_server
        from radioinspector.windows.client import PlatformClient
        investigation = self.slow_active()
        server = create_server(host="127.0.0.1", port=0, sessions=self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = PlatformClient(f"http://127.0.0.1:{server.server_port}")
            data = client.get_investigation(self.session.id, investigation.id)
            self.assertEqual(data["started_at"], to_primitive(BASE))
            self.assertEqual(data["listening_started_at"], to_primitive(investigation.listening_started_at))
            from radioinspector.windows.app import investigation_row
            investigation_row(data)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_contract_optional_roundtrip(self):
        investigation = self.slow_active()
        data = to_primitive(investigation)
        self.assertEqual(Investigation.from_dict(data), investigation)
        self.assertTrue(data["listening_started_at"].endswith("Z"))
        del data["listening_started_at"]
        self.assertIsNone(Investigation.from_dict(data).listening_started_at)


class SQLiteListeningDurationTests(ListeningDurationTests):
    def make_service(self):
        self.now = BASE
        return SessionService(clock=lambda: self.now, store=SQLiteSessionStore(":memory:"))

    def test_listening_save_failure_rolls_back_origin(self):
        store = self.service._store
        original = store._insert_event
        def fail_listening(event):
            if event.event_type == "investigation.listening":
                raise RuntimeError("synthetic listening persistence failure")
            return original(event)
        with (
            patch.object(store, "_insert_event", side_effect=fail_listening),
            self.assertRaisesRegex(RuntimeError, "synthetic listening persistence"),
        ):
            self.active()
        current = self.service.get_investigation(self.session.id, "investigation-active")
        self.assertEqual(current.state, InvestigationState.TUNING)
        self.assertIsNone(current.listening_started_at)
        saved = store.load_investigations()[0][1]
        self.assertEqual(saved, current)
        self.assertFalse(any(e.event_type == "investigation.listening" for e in self.service.events()))

    def test_store_roundtrip_and_restart(self):
        investigation = self.slow_active()
        store = self.service._store
        saved = store.load_investigations()[0][1]
        self.assertEqual(saved, investigation)
        restored = SessionService(clock=lambda: self.now, store=store)
        current = restored.get_investigation(self.session.id, investigation.id)
        self.assertEqual(current.state, InvestigationState.FAILED)
        self.assertEqual(current.listening_started_at, investigation.listening_started_at)
        self.assertEqual(current.started_at, BASE)
        self.assertFalse(self.scheduler.status(self.session.id)["policy_persistent"])


class ListeningMigrationTests(unittest.TestCase):
    def legacy(self, path):
        store = SQLiteSessionStore(str(path))
        fixture = CompletionSetup()
        fixture.make_service = lambda: SessionService(store=store, clock=lambda: fixture.now)
        fixture.now = BASE
        fixture.setup_scheduler()
        investigation = fixture.active()
        fixture.observations(investigation)
        fixture.service.complete_investigation(fixture.session.id, investigation.id)
        from tests.test_measurement_results import payload
        from tests.test_sweep_comparison import measurement
        fixture.service.save_measurement_result(fixture.session.id, payload(stream_id=fixture.spectrum_id))
        fixture.service.ingest_sweep(measurement(fixture.session.id, fixture.spectrum_id))
        fixture.service.create_sweep_reference(fixture.session.id, {"name": "migration baseline", "sweep_id": "sweep-0"})
        fixture.scheduler.close()
        # Owned synthetic database only: reconstruct the previous nullable-column schema.
        store._connection.execute("ALTER TABLE investigations DROP COLUMN listening_started_at")
        store._connection.execute("PRAGMA user_version = 4")
        store._connection.commit()
        store.close()

    def test_migration_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owned.sqlite3"
            self.legacy(path)
            with closing(sqlite3.connect(path)) as connection:
                before = self.archive_fixture_values(connection)
            with (
                patch.object(SQLiteSessionStore, "_create_reference_schema", side_effect=RuntimeError("synthetic migration failure")),
                self.assertRaisesRegex(RuntimeError, "synthetic migration"),
            ):
                SQLiteSessionStore(str(path))
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(self.archive_fixture_values(connection), before)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertNotIn("listening_started_at", [r[1] for r in connection.execute("PRAGMA table_info(investigations)")])
            with SQLiteSessionStore(str(path)) as store:
                self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertIn("listening_started_at", [r[1] for r in store._connection.execute("PRAGMA table_info(investigations)")])
                legacy = store.load_investigations()[0][1]
                self.assertIsNone(legacy.listening_started_at)
                self.assertEqual(legacy.started_at, BASE)
                self.assertEqual(legacy.state, InvestigationState.COMPLETED)

    @staticmethod
    def archive_fixture_values(connection):
        # Fixed projections of the owned fixture, not a generic archive dump.
        queries = {
            "session": "SELECT id, state, started_at, metadata_json FROM sessions ORDER BY id",
            "stream_units": "SELECT id, role, level_unit, settings_json FROM streams ORDER BY id",
            "candidate": "SELECT id, started_at, ended_at, peak_level, level_unit FROM candidates WHERE id = 'candidate-active'",
            "investigation": "SELECT id, state, started_at, ended_at, result_observation_ids_json FROM investigations WHERE id = 'investigation-active'",
            "sweep_samples": "SELECT id, frequencies_json, levels_json, level_unit FROM sweeps WHERE id = 'sweep-0'",
            "sweep_time": "SELECT id, started_at, ended_at, settings_json FROM sweeps WHERE id = 'sweep-0'",
            "observation": "SELECT id, started_at, ended_at, level, level_unit FROM observations ORDER BY id",
            "result_values": "SELECT id, methodology, result_code, values_json FROM measurement_results ORDER BY id",
            "result_time": "SELECT id, frequency_hz, started_at, ended_at, created_at FROM measurement_results ORDER BY id",
            "result_owner": "SELECT id, session_id, stream_id FROM measurement_results ORDER BY id",
            "reference_source": "SELECT id, session_id, stream_id, sweep_id, name FROM sweep_references WHERE name = 'migration baseline'",
            "reference_values": "SELECT id, created_at, started_at, ended_at, level_unit, points FROM sweep_references WHERE name = 'migration baseline'",
            "events": "SELECT sequence, event_type, occurred_at, payload_json FROM events ORDER BY sequence",
            "indices": "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' ORDER BY name",
        }
        return {key: [tuple(row) for row in connection.execute(query)] for key, query in queries.items()}

    def test_migration_preserves_explicit_archive_fixture_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owned-preservation.sqlite3"
            self.legacy(path)
            with closing(sqlite3.connect(path)) as connection:
                before = self.archive_fixture_values(connection)
            with SQLiteSessionStore(str(path)) as store:
                self.assertEqual(self.archive_fixture_values(store._connection), before)
                self.assertEqual(
                    store._connection.execute("SELECT COUNT(*) FROM investigations WHERE listening_started_at IS NOT NULL").fetchone()[0],
                    0,
                )

    def test_future_schema_rejected_without_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            self.legacy(path)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
                before = self.archive_fixture_values(connection)
            before_bytes = path.read_bytes()
            with self.assertRaises(ValueError):
                SQLiteSessionStore(str(path))
            self.assertEqual(path.read_bytes(), before_bytes)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(self.archive_fixture_values(connection), before)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1)
                self.assertNotIn("listening_started_at", [row[1] for row in connection.execute("PRAGMA table_info(investigations)")])


class MigrationConnectionLifetimeTests(unittest.TestCase):
    def test_migrations_close_connections_before_directory_cleanup(self):
        from contextlib import contextmanager

        original_connect = sqlite3.connect
        original_directory = tempfile.TemporaryDirectory
        methods = (
            "test_migration_and_rollback",
            "test_migration_preserves_explicit_archive_fixture_values",
            "test_future_schema_rejected_without_change",
        )
        for method in methods:
            with self.subTest(method=method):
                connections = []  # Strong references prevent GC from hiding leaks.
                cleanup_checked = []

                def connect(*args, connections=connections, **kwargs):
                    connection = original_connect(*args, **kwargs)
                    connections.append(connection)
                    return connection

                def assert_closed(connections=connections):
                    self.assertTrue(connections)
                    for connection in connections:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            connection.execute("SELECT 1")

                @contextmanager
                def directory(*args, connections=connections, cleanup_checked=cleanup_checked, assert_closed=assert_closed, **kwargs):
                    with original_directory(*args, **kwargs) as path:
                        try:
                            yield path
                            assert_closed()  # Must precede filesystem cleanup on Windows.
                            cleanup_checked.append(True)
                        finally:
                            # Keep the regression itself safe when exercising broken code.
                            for connection in connections:
                                connection.close()
                    self.assertFalse(Path(path).exists())
                    assert_closed()

                with (
                    patch.object(sqlite3, "connect", side_effect=connect),
                    patch.object(tempfile, "TemporaryDirectory", side_effect=directory),
                ):
                    case = ListeningMigrationTests(method)
                    getattr(case, method)()
                self.assertEqual(cleanup_checked, [True])
