"""Logical SQLite capacity guard: atomic refusal, status and HTTP/CLI contract."""

from __future__ import annotations

import http.client
import io
import json
import queue
from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from radioinspector.cli import main
from radioinspector.contracts import ReceiverObservation, SessionEvent, Sweep, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import ArchiveCapacityExceededError, SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import BASE, session_payload


class ArchiveCapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store)
        self.addCleanup(lambda: self.service.close())
        self.session = self.service.create_session(session_payload())
        self.spectrum = self.session.streams[0].id
        self.receiver = self.session.streams[1].id

    def reopen(self, limit: int | None) -> None:
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=limit)
        self.service = SessionService(store=self.store)

    def payload(self, kind: str, number: int = 0, padding: int = 0) -> dict:
        if kind == "sweeps":
            payload = sweep_payload(self.session.id, self.spectrum, number)
            payload["settings"]["padding"] = "x" * padding
        else:
            payload = observation_payload(self.session.id, self.receiver, number)
            payload["metadata"]["padding"] = "x" * padding
        return payload

    def ingest(self, kind: str, items: list[dict], batch: bool = True):
        method = "ingest_" + (kind if batch else kind[:-1])
        return getattr(self.service, method)(items if batch else items[0])

    def test_unlimited_default_and_memory_status_are_honest(self) -> None:
        status = self.service.archive_capacity()
        with self.store._lock:
            pages = self.store._connection.execute("PRAGMA page_count").fetchone()[0]
            page_size = self.store._connection.execute("PRAGMA page_size").fetchone()[0]
        self.assertEqual(status["database_bytes"], pages * page_size)
        self.assertEqual(status["state"], "unlimited")
        self.assertEqual(status["storage_mode"], "sqlite")
        self.assertIsNone(status["max_database_bytes"])
        self.assertIsNone(status["remaining_bytes"])
        self.assertFalse(status["measurement_writes_blocked"])
        self.assertFalse(status["automatic_deletion"])
        self.service.ingest_sweeps([self.payload("sweeps", padding=200_000)])
        self.assertGreater(self.service.archive_capacity()["database_bytes"], status["database_bytes"])
        memory = SessionService()
        self.addCleanup(memory.close)
        memory_status = memory.archive_capacity()
        self.assertEqual(memory_status["storage_mode"], "memory")
        self.assertEqual(memory_status["state"], "unavailable")
        for key in ("database_bytes", "max_database_bytes", "remaining_bytes"):
            self.assertIsNone(memory_status[key])
        self.assertFalse(memory_status["measurement_writes_blocked"])
        for service in (self.service, memory):
            features = service.capabilities()["features"]
            self.assertTrue(features["archive_capacity_guard"])
            self.assertFalse(features["archive_automatic_deletion"])

    def test_exact_thresholds_and_large_integer_arithmetic(self) -> None:
        self.reopen(10_000)
        for size, state in ((8_999, "ok"), (9_000, "warning"), (9_999, "warning"),
                            (10_000, "full"), (10_001, "full")):
            with self.subTest(size=size), patch.object(
                self.store, "_database_bytes_locked", return_value=size
            ):
                status = self.store.archive_capacity()
                self.assertEqual(status["state"], state)
                self.assertEqual(status["remaining_bytes"], max(0, 10_000 - size))
                self.assertEqual(status["measurement_writes_blocked"], state == "full")
                self.assertEqual(status["warning_threshold"], 0.9)
        huge = 10**1000
        self.reopen(huge)
        with patch.object(self.store, "_database_bytes_locked", return_value=huge * 9 // 10):
            self.assertEqual(self.store.archive_capacity()["state"], "warning")
        with patch.object(self.store, "_database_bytes_locked", return_value=huge * 9 // 10 - 1):
            self.assertEqual(self.store.archive_capacity()["state"], "ok")

    def test_single_and_batch_rollback_preserves_measurements_events_and_subscribers(self) -> None:
        size = self.service.archive_capacity()["database_bytes"]
        self.reopen(size + 4096)
        for kind in ("sweeps", "observations"):
            for batch in (False, True):
                with self.subTest(kind=kind, batch=batch):
                    before = self.service.archive_capacity()
                    health = self.service.health()
                    sessions = self.service.list_sessions()
                    events = self.service.events()
                    subscriber_id, subscriber = self.service.subscribe(after=health["last_event_id"])
                    items = [self.payload(kind, padding=100_000)]
                    if batch:
                        items.insert(0, self.payload(kind, 1))
                    with self.assertRaises(ArchiveCapacityExceededError):
                        self.ingest(kind, items, batch=batch)
                    self.assertEqual(self.service.archive_capacity(), before)
                    self.assertEqual(self.service.health(), health)
                    self.assertEqual(self.service.list_sessions(), sessions)
                    self.assertEqual(self.service.events(), events)
                    self.assertEqual(self.service.list_sweeps(self.session.id), [])
                    self.assertEqual(self.service.list_observations(self.session.id), [])
                    with self.assertRaises(queue.Empty):
                        subscriber.get_nowait()
                    self.service.unsubscribe(subscriber_id)
        # A smaller subsequent write reuses the cursor, with no gap from refusal.
        accepted = self.service.ingest_sweep(self.payload("sweeps", 10))
        self.assertEqual(accepted.id, "sweep-10")
        self.assertEqual(self.service.events()[-1].sequence, 2)

    def test_events_are_included_in_post_write_capacity_check(self) -> None:
        size = self.service.archive_capacity()["database_bytes"]
        self.reopen(size + 4096)
        original = self.service._make_event_locked

        def oversized_event(*args, **kwargs):
            kwargs["payload"] = {"large_event": "x" * 100_000}
            return original(*args, **kwargs)

        with patch.object(self.service, "_make_event_locked", side_effect=oversized_event):
            with self.assertRaises(ArchiveCapacityExceededError):
                self.service.ingest_sweep(self.payload("sweeps"))
        self.assertEqual(self.service.list_sweeps(self.session.id), [])
        self.assertEqual(self.service.health()["last_event_id"], 1)
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.store.archive_capacity()["database_bytes"], size)

    def test_optional_event_single_store_writes_use_the_same_guard(self) -> None:
        self.reopen(self.store.archive_capacity()["database_bytes"] + 4096)
        for kind, model, write in (
            ("sweeps", Sweep, self.store.record_sweep),
            ("observations", ReceiverObservation, self.store.record_observation),
        ):
            with self.subTest(kind=kind):
                write(model.from_dict(self.payload(kind)))
                with self.assertRaises(ArchiveCapacityExceededError):
                    write(model.from_dict(self.payload(kind, 1, 100_000)))
        self.assertEqual(len(self.store.list_sweeps(self.session.id)), 1)
        self.assertEqual(len(self.store.list_observations(self.session.id)), 1)
        self.assertEqual(len(self.store.events()), 1)
        with self.assertRaises(ValueError):
            self.store.record_sweeps([Sweep.from_dict(self.payload("sweeps", 2))], events=[])
        with self.assertRaises(ValueError):
            self.store.record_observations(
                [ReceiverObservation.from_dict(self.payload("observations", 2))], events=[]
            )

    def test_full_archive_rejects_before_mutating(self) -> None:
        size = self.store.archive_capacity()["database_bytes"]
        self.reopen(size)
        with patch.object(self.store, "_insert_sweep") as insert:
            with self.assertRaises(ArchiveCapacityExceededError):
                self.service.ingest_sweep(self.payload("sweeps"))
            insert.assert_not_called()
        self.assertEqual(self.service.archive_capacity()["state"], "full")

    def test_logical_size_includes_freelist_not_just_measurement_rows(self) -> None:
        initial = self.store.archive_capacity()["database_bytes"]
        with self.store._lock, self.store._connection:
            self.store._connection.execute("CREATE TABLE capacity_test_padding (data BLOB)")
            self.store._connection.execute(
                "INSERT INTO capacity_test_padding VALUES (zeroblob(200000))"
            )
            self.store._connection.execute("DROP TABLE capacity_test_padding")
        with self.store._lock:
            free_pages = self.store._connection.execute("PRAGMA freelist_count").fetchone()[0]
        size = self.store.archive_capacity()["database_bytes"]
        self.assertGreater(free_pages, 0)
        self.assertGreater(size, initial)
        self.reopen(size)
        self.assertEqual(self.store.archive_capacity()["database_bytes"], size)
        with self.assertRaises(ArchiveCapacityExceededError):
            self.service.ingest_sweep(self.payload("sweeps"))

    def test_status_cannot_observe_uncommitted_rejected_growth(self) -> None:
        before = self.store.archive_capacity()["database_bytes"]
        self.reopen(before + 4096)
        inserted = threading.Event()
        release = threading.Event()
        reader_started = threading.Event()
        original = self.store._insert_event

        def pause_after_insert(event):
            original(event)
            inserted.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release transaction")

        def read_status():
            reader_started.set()
            return self.store.archive_capacity()

        with patch.object(self.store, "_insert_event", side_effect=pause_after_insert):
            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(self.service.ingest_sweep, self.payload("sweeps", padding=100_000))
                try:
                    self.assertTrue(inserted.wait(timeout=5))
                    reader = pool.submit(read_status)
                    self.assertTrue(reader_started.wait(timeout=5))
                    self.assertFalse(reader.done())
                finally:
                    release.set()
                with self.assertRaises(ArchiveCapacityExceededError):
                    writer.result(timeout=5)
                self.assertEqual(reader.result(timeout=5)["database_bytes"], before)

    def test_concurrent_writers_compete_for_last_slot_under_store_lock(self) -> None:
        # Calibrate real SQLite page growth on a backup rather than assume a page count.
        probe_path = str(Path(self.path).with_name("probe.sqlite"))
        with closing(sqlite3.connect(probe_path)) as destination, destination:
            self.store._connection.backup(destination)
        probe = SQLiteSessionStore(probe_path)
        first = Sweep.from_dict(self.payload("sweeps", 1, 50_000))
        event = SessionEvent(sequence=2, event_type="sweep.received", occurred_at=BASE,
                             session_id=self.session.id, payload={"sweep": to_primitive(first)})
        probe.record_sweep(first, event=event)
        limit = probe.archive_capacity()["database_bytes"]
        probe.close()
        self.reopen(limit)
        barrier = threading.Barrier(2)

        def write(number: int) -> bool:
            item = Sweep.from_dict(self.payload("sweeps", number, 50_000))
            next_event = SessionEvent(sequence=number + 1, event_type="sweep.received",
                                      occurred_at=BASE, session_id=self.session.id,
                                      payload={"sweep": to_primitive(item)})
            barrier.wait(timeout=5)
            try:
                self.store.record_sweep(item, event=next_event)
                return True
            except ArchiveCapacityExceededError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, (1, 2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.store.list_sweeps(self.session.id)), 1)
        self.assertEqual(len(self.store.events()), 2)
        self.assertEqual(self.store.archive_capacity()["database_bytes"], limit)
        self.assertEqual(self.store.archive_capacity()["state"], "full")

    def test_independent_connections_serialize_the_shared_final_budget(self) -> None:
        probe_path = str(Path(self.path).with_name("independent-probe.sqlite"))
        with closing(sqlite3.connect(probe_path)) as destination, destination:
            self.store._connection.backup(destination)
        first = Sweep.from_dict(self.payload("sweeps", 1, 50_000))
        second = Sweep.from_dict(self.payload("sweeps", 2, 50_000))
        with SQLiteSessionStore(probe_path) as probe:
            probe.record_sweep(first)
            limit = probe.archive_capacity()["database_bytes"]
        self.reopen(limit)
        other = SQLiteSessionStore(self.path, max_database_bytes=limit)
        self.addCleanup(other.close)
        self.assertIsNot(self.store._lock, other._lock)
        self.assertIsNot(self.store._connection, other._connection)
        inserted = threading.Event()
        second_begin = threading.Event()
        release = threading.Event()
        original = self.store._insert_sweep

        def hold_first_write(item):
            original(item)
            inserted.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release first writer")

        def trace_other_connection(statement):
            if statement == "BEGIN IMMEDIATE":
                second_begin.set()

        other._connection.set_trace_callback(trace_other_connection)
        with patch.object(self.store, "_insert_sweep", side_effect=hold_first_write):
            with ThreadPoolExecutor(max_workers=2) as pool:
                accepted = pool.submit(self.store.record_sweep, first)
                try:
                    self.assertTrue(inserted.wait(timeout=5))
                    rejected = pool.submit(other.record_sweep, second)
                    # This connection has reached BEGIN IMMEDIATE while the first
                    # connection owns the writer. No shared Python lock can help.
                    self.assertTrue(second_begin.wait(timeout=5))
                    self.assertFalse(rejected.done())
                finally:
                    release.set()
                accepted.result(timeout=5)
                with self.assertRaises(ArchiveCapacityExceededError):
                    rejected.result(timeout=5)
        other._connection.set_trace_callback(None)
        for store in (self.store, other):
            with self.subTest(connection=store is other):
                status = store.archive_capacity()
                self.assertEqual(status["database_bytes"], limit)
                self.assertEqual(status["state"], "full")
                self.assertEqual(status["remaining_bytes"], 0)
                self.assertEqual(store.list_sweeps(self.session.id), [first])
                self.assertEqual(len(store.events()), 1)
                self.assertFalse(store._connection.in_transaction)
        self.assertEqual(self.store.archive_capacity(), other.archive_capacity())

    def test_mixed_writers_share_one_budget_and_restart_preserves_data(self) -> None:
        self.reopen(self.store.archive_capacity()["database_bytes"] + 100_000)
        accepted = []
        rejected = []
        for number in range(10):
            kind = "sweeps" if number % 2 == 0 else "observations"
            try:
                self.ingest(kind, [self.payload(kind, number, 8_000)], batch=False)
                accepted.append(kind)
            except ArchiveCapacityExceededError:
                rejected.append(kind)
        self.assertIn("sweeps", accepted)
        self.assertIn("observations", accepted)
        self.assertTrue(rejected)
        limit = self.store.max_database_bytes
        before = self.service.archive_capacity()
        self.assertLessEqual(before["database_bytes"], limit)
        self.reopen(limit)
        self.assertEqual(self.service.archive_capacity(), before)
        self.assertEqual(len(self.service.list_sweeps(self.session.id)), accepted.count("sweeps"))
        self.assertEqual(len(self.service.list_observations(self.session.id)), accepted.count("observations"))
        self.assertEqual(self.service.health()["last_event_id"], 1 + len(accepted))
        self.assertEqual(self.service.list_observations(self.session.id)[0].level_unit, "s_meter_count")

    def test_metadata_and_lifecycle_continue_when_archive_is_already_over_limit(self) -> None:
        self.service.ingest_sweep(self.payload("sweeps", padding=10_000))
        size = self.store.archive_capacity()["database_bytes"]
        self.reopen(size - 1)
        self.assertEqual(self.service.archive_capacity()["remaining_bytes"], 0)
        self.service.start_session(self.session.id)
        self.service.publish("operator.note", session_id=self.session.id,
                             payload={"note": "x" * 50_000})
        self.service.stop_session(self.session.id)
        second = self.service.create_session(session_payload())
        self.assertIsNotNone(self.service.get_session(second.id))
        self.assertGreater(self.service.archive_capacity()["database_bytes"], size)
        self.assertEqual(len(self.service.list_sweeps(self.session.id)), 1)
        self.assertEqual(self.service.get_session(self.session.id).state.value, "stopped")
        with self.assertRaises(ArchiveCapacityExceededError):
            self.service.ingest_observation(self.payload("observations"))


class ArchiveCapacityHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(self.path))
        self.session = service.create_session(session_payload())
        service.ingest_sweep(sweep_payload(self.session.id, self.session.streams[0].id))
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0,
                                    session_database=self.path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, method: str, path: str, payload=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=None if payload is None else json.dumps(payload),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body)
        finally:
            connection.close()

    def test_capacity_route_and_all_four_ingest_routes_return_stable_507(self) -> None:
        status, envelope = self.request("GET", "/api/v1/archive/capacity")
        self.assertEqual(status, 200)
        self.assertEqual(envelope["contract_version"], 1)
        capacity = envelope["data"]
        self.assertEqual(set(capacity), {"storage_mode", "database_bytes", "max_database_bytes",
                         "remaining_bytes", "state", "warning_threshold",
                         "measurement_writes_blocked", "automatic_deletion"})
        self.assertEqual(capacity["state"], "full")
        self.assertTrue(capacity["measurement_writes_blocked"])
        self.assertNotIn(self.path, json.dumps(envelope))
        before = self.server.session_service.health()
        for kind, factory, stream in (("sweeps", sweep_payload, self.session.streams[0]),
                                       ("observations", observation_payload, self.session.streams[1])):
            for batch in (False, True):
                item = factory(self.session.id, stream.id, 10)
                path = f"/api/v1/sessions/{self.session.id}/{kind}" + ("/batch" if batch else "")
                status, response = self.request("POST", path, {"items": [item]} if batch else item)
                self.assertEqual(status, 507)
                self.assertEqual(response["error"]["code"], "archive_capacity_exceeded")
                self.assertNotIn(self.path, json.dumps(response))
        self.assertEqual(self.server.session_service.health(), before)
        status, capabilities = self.request("GET", "/api/v1/capabilities")
        self.assertTrue(capabilities["data"]["features"]["archive_capacity_guard"])
        self.assertFalse(capabilities["data"]["features"]["archive_automatic_deletion"])

    def test_export_and_lifecycle_remain_available_when_full(self) -> None:
        root = f"/api/v1/sessions/{self.session.id}"
        for suffix in ("", "/sweeps", "/observations", "/archive/export"):
            status, _ = self.request("GET", root + suffix)
            self.assertEqual(status, 200)
        status, exported = self.request("GET", root + "/archive/export")
        self.assertEqual(len(exported["data"]["sweeps"]), 1)
        for action in ("start", "stop"):
            status, _ = self.request("POST", root + "/" + action, {})
            self.assertEqual(status, 200)
        status, health = self.request("GET", "/api/v1/health")
        self.assertEqual(health["data"]["status"], "ok")


class ArchiveCapacityConfigurationTests(unittest.TestCase):
    def test_invalid_store_options_do_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "invalid.sqlite")
            for invalid in (True, False, 0, -1, 1.5, "4096"):
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    SQLiteSessionStore(path, max_database_bytes=invalid)
                self.assertFalse(Path(path).exists())

    def test_cli_rejects_invalid_values_and_missing_database(self) -> None:
        for options in (("--archive-max-bytes", "1"),
                        ("--session-database", "x", "--archive-max-bytes", "0"),
                        ("--session-database", "x", "--archive-max-bytes", "-1"),
                        ("--session-database", "x", "--archive-max-bytes", "1.5")):
            with self.subTest(options=options), redirect_stderr(io.StringIO()), patch(
                "radioinspector.web.server.serve"
            ) as serve:
                with self.assertRaises(SystemExit) as caught:
                    main(["serve", *options])
                self.assertEqual(caught.exception.code, 2)
                serve.assert_not_called()
        with patch("radioinspector.web.server.serve") as serve:
            self.assertEqual(main(["serve", "--session-database", "x", "--archive-max-bytes", "4096"]), 0)
            self.assertEqual(serve.call_args.kwargs["archive_max_bytes"], 4096)
        with patch("radioinspector.web.server.serve") as serve:
            self.assertEqual(main(["serve"]), 0)
            self.assertIsNone(serve.call_args.kwargs["archive_max_bytes"])

    def test_invalid_server_combinations_and_startup_failure_cleanup(self) -> None:
        memory = SessionService()
        self.addCleanup(memory.close)
        for options in ({"archive_max_bytes": 4096}, {"sessions": memory, "archive_max_bytes": 4096},
                        {"sessions": memory, "session_database": "x", "archive_max_bytes": 4096}):
            with self.assertRaises(ValueError):
                create_server(port=0, **options)
        created = []

        def capture_store(*args, **kwargs):
            store = SQLiteSessionStore(*args, **kwargs)
            created.append(store)
            return store

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "archive.sqlite")
            with patch("radioinspector.web.server.SQLiteSessionStore", side_effect=capture_store), patch(
                "radioinspector.web.server.ThreadingHTTPServer", side_effect=OSError("bind failed")
            ):
                with self.assertRaisesRegex(OSError, "bind failed"):
                    create_server(port=0, session_database=path, archive_max_bytes=4096)
            self.assertEqual(len(created), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                created[0].archive_capacity()
        # Caller-owned services must not be closed when startup fails.
        with patch("radioinspector.web.server.ThreadingHTTPServer", side_effect=OSError("bind failed")):
            with self.assertRaises(OSError):
                create_server(port=0, sessions=memory)
        self.assertEqual(memory.health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
