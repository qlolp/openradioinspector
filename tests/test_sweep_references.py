"""Named archive references: validation, atomic storage, restart, concurrency, HTTP."""

from __future__ import annotations

import copy
import http.client
import json
import queue
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from radioinspector.contracts import to_primitive
from radioinspector.session_service import SessionNotFoundError, SessionService, SweepNotFoundError
from radioinspector.session_store import ArchiveCapacityExceededError, SCHEMA_VERSION, SQLiteSessionStore
from radioinspector.sweep_comparison import ComparisonIncompatibleError, MAX_COMPARISON_POINTS
from radioinspector.sweep_references import (
    MAX_SWEEP_REFERENCES, ReferenceLimitReachedError, ReferenceNameConflictError,
    normalize_reference_name,
)
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session, measurement
from tests.test_platform import BASE, session_payload


class ReferenceNameTests(unittest.TestCase):
    def test_nfc_whitespace_case_and_code_points(self):
        self.assertEqual(normalize_reference_name(" \u2003Cafe\u0301\u2028"), "Café")
        self.assertEqual(normalize_reference_name("BASE"), "BASE")
        self.assertEqual(normalize_reference_name("😀" * 80), "😀" * 80)
        self.assertEqual(normalize_reference_name("  x  y  "), "x  y")

    def test_rejects_invalid_types_lengths_and_every_c_category_before_trim(self):
        for value in (None, True, 1, [], {}, "", "  ", "\u2003", "x" * 81,
                      "😀" * 81, "x\n", "\tx", "x\x7f", "x\x85", "x\u200d",
                      "x\ufeff", "x\ud800", "x\ue000", "x\u0378"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                normalize_reference_name(value)


class ReferenceServiceMixin:
    def add(self, number=0, **changes):
        return self.service.ingest_sweep(measurement(self.session.id, changes.pop("stream_id", self.stream_id), number, **changes))

    def save(self, name="Baseline", sweep_id="sweep-0"):
        return self.service.create_sweep_reference(self.session.id, {"name": name, "sweep_id": sweep_id})

    def catalog(self, stream_id=None):
        return self.service.list_sweep_references(self.session.id, stream_id=stream_id)

    def test_record_provenance_and_independent_returns(self):
        sweep = self.add()
        before_sweeps = self.service.list_sweeps(self.session.id)
        record, created = self.save("  Cafe\u0301  ")
        self.assertTrue(created)
        self.assertEqual(set(record), {"id", "session_id", "stream_id", "sweep_id", "name",
                                      "created_at", "interval", "level_unit", "points"})
        self.assertEqual(record["session_id"], self.session.id)
        self.assertEqual(record["stream_id"], self.stream_id)
        self.assertEqual(record["sweep_id"], sweep.id)
        self.assertEqual(record["name"], "Café")
        self.assertEqual(record["interval"], to_primitive(sweep.interval))
        self.assertEqual(record["level_unit"], "dBm")
        self.assertEqual(record["points"], 4)
        expected = copy.deepcopy(record)
        record["name"] = "edited"
        record["interval"]["started_at"] = "edited"
        self.assertEqual(self.catalog()["items"], [expected])
        result = self.catalog()
        result["items"][0]["name"] = "edited"
        self.assertEqual(self.catalog()["items"], [expected])
        self.assertEqual(before_sweeps, self.service.list_sweeps(self.session.id))
        self.assertEqual(self.service.compare_sweeps(
            self.session.id, reference_id=expected["sweep_id"], target_id=sweep.id,
        )["delta_levels"], [0] * 4)

    def test_idempotency_normalization_conflicts_and_single_event(self):
        self.add()
        self.add(1)
        before = self.service.health()["last_event_id"]
        subscriber_id, events = self.service.subscribe(after=before)
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        record, created = self.save("  Cafe\u0301 ")
        event = events.get_nowait()
        self.assertTrue(created)
        self.assertEqual(event.event_type, "sweep_reference.created")
        self.assertEqual(event.payload, {"reference": record})
        self.assertEqual(event.session_id, self.session.id)
        self.assertEqual(event.sequence, before + 1)
        self.assertEqual(self.save("Café"), (record, False))
        with self.assertRaises(ReferenceNameConflictError):
            self.save("Café", "sweep-1")
        self.assertEqual(self.service.health()["last_event_id"], before + 1)
        with self.assertRaises(queue.Empty):
            events.get_nowait()
        self.assertTrue(self.save("café")[1])
        self.assertTrue(self.save("Different label")[1])

    def test_catalog_scope_envelope_capability_and_read_only(self):
        self.add()
        self.add(1, stream_id=self.session.streams[1].id)
        a, _ = self.save("a")
        b, _ = self.save("b", "sweep-1")
        before = (self.service.health(), self.service.events())
        catalog = self.catalog()
        self.assertEqual(set(catalog), {"session_id", "stream_id", "storage_mode", "persistent", "limit", "items"})
        self.assertEqual(catalog["session_id"], self.session.id)
        self.assertIsNone(catalog["stream_id"])
        self.assertEqual(catalog["persistent"], self.service._store is not None and self.service._store.persistent)
        self.assertEqual(catalog["storage_mode"], "sqlite" if self.service._store else "memory")
        self.assertEqual(catalog["limit"], 100)
        self.assertEqual(catalog["items"], [a, b])
        self.assertEqual(self.catalog(self.stream_id)["items"], [a])
        self.assertEqual(self.catalog(self.session.streams[1].id)["items"], [b])
        other = create_session(self.service)
        self.assertEqual(self.service.list_sweep_references(other.id)["items"], [])
        self.assertTrue(self.service.capabilities()["features"]["named_sweep_references"])
        # Only explicit creation of the other session changed the event cursor.
        self.assertEqual(self.service.health()["last_event_id"], before[0]["last_event_id"] + 1)

    def test_limit_per_session_replay_at_capacity_and_name_conflict_precedence(self):
        self.add()
        self.add(1)
        records = [self.save(f"label-{i}")[0] for i in range(MAX_SWEEP_REFERENCES)]
        before = self.service.health()["last_event_id"]
        self.assertEqual(self.save("label-0"), (records[0], False))
        with self.assertRaises(ReferenceNameConflictError):
            self.save("label-0", "sweep-1")
        with self.assertRaises(ReferenceLimitReachedError):
            self.save("one-too-many")
        self.assertEqual(len(self.catalog()["items"]), MAX_SWEEP_REFERENCES)
        self.assertEqual(self.service.health()["last_event_id"], before)
        other = create_session(self.service)
        self.service.ingest_sweep(measurement(other.id, other.streams[0].id, 99))
        self.assertTrue(self.service.create_sweep_reference(other.id, {"name": "label-0", "sweep_id": "sweep-99"})[1])

    def test_concurrent_replay_and_session_cap(self):
        self.add()
        before = self.service.health()["last_event_id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.save(), range(24)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({record["id"] for record, _ in results}), 1)
        self.assertEqual(self.service.health()["last_event_id"], before + 1)
        for index in range(MAX_SWEEP_REFERENCES - 2):
            self.save(f"ref-{index}")
        def add_last(index):
            try:
                return self.save(f"last-{index}")[1]
            except ReferenceLimitReachedError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(add_last, range(16))), 1)
        self.assertEqual(len(self.catalog()["items"]), MAX_SWEEP_REFERENCES)

    def test_reference_outside_recent_sweep_and_event_windows(self):
        for start, stop in ((0, 100), (100, 112)):
            self.service.ingest_sweeps([
                measurement(self.session.id, self.stream_id, i) for i in range(start, stop)
            ])
        self.assertNotIn("sweep-0", [s.id for s in self.service.list_sweeps(self.session.id)])
        record, created = self.save()
        self.assertTrue(created)
        self.assertEqual(record["sweep_id"], "sweep-0")
        self.assertEqual(self.service.compare_sweeps(
            self.session.id, reference_id=record["sweep_id"], target_id="sweep-111",
        )["points"], 4)

    def test_invalid_bodies_missing_foreign_ids_and_queries(self):
        self.add()
        for payload in (None, [], "x", {}, {"name": "x"}, {"sweep_id": "sweep-0"},
                        {"name": "x", "sweep_id": "sweep-0", "stream_id": self.stream_id},
                        {"name": 1, "sweep_id": "sweep-0"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.create_sweep_reference(self.session.id, payload)
        for sweep_id in (None, "", "   ", True, 1):
            with self.subTest(sweep_id=sweep_id), self.assertRaises(ValueError):
                self.save(sweep_id=sweep_id)
        other = create_session(self.service)
        self.service.ingest_sweep(measurement(other.id, other.streams[0].id, 9))
        for sweep_id in ("missing", "sweep-9", "' OR 1=1 --"):
            with self.subTest(sweep_id=sweep_id), self.assertRaises(SweepNotFoundError):
                self.save(sweep_id=sweep_id)
        with self.assertRaises(SessionNotFoundError):
            self.service.create_sweep_reference("missing", {"name": "x", "sweep_id": "sweep-0"})
        with self.assertRaises(SessionNotFoundError):
            self.service.list_sweep_references("missing")
        for stream in ("missing", "", "  ", other.streams[0].id, "' OR 1=1 --"):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                self.catalog(stream)
        self.assertEqual(self.catalog()["items"], [])

    def test_scientific_eligibility_reuses_comparison_rules_without_events(self):
        for index, settings in enumerate(({}, {"sequence": 1}, {"rbw_hz": None}, {"nested": [None]})):
            self.add(index, settings=settings)
            before = self.service.health()["last_event_id"]
            with self.subTest(settings=settings), self.assertRaises(ComparisonIncompatibleError):
                self.save(sweep_id=f"sweep-{index}")
            self.assertEqual(self.service.health()["last_event_id"], before)
        for count in (MAX_COMPARISON_POINTS, MAX_COMPARISON_POINTS + 1):
            self.add(count, frequencies_hz=list(range(count)), levels=[-80] * count)
            if count > MAX_COMPARISON_POINTS:
                with self.assertRaises(ComparisonIncompatibleError):
                    self.save("too large", f"sweep-{count}")
            else:
                self.assertEqual(self.save("max", f"sweep-{count}")[0]["points"], count)

    def test_settings_remain_immutable_and_mismatch_still_rejected(self):
        self.add()
        self.add(1, settings={"rbw_hz": 20_000})
        record, _ = self.save()
        with self.assertRaises(ComparisonIncompatibleError):
            self.service.compare_sweeps(self.session.id, reference_id=record["sweep_id"], target_id="sweep-1")
        self.assertEqual(self.catalog()["items"], [record])

    def test_subsecond_catalog_order_is_chronological(self):
        self.add()
        with patch.object(self.service, "_clock", return_value=BASE):
            first, _ = self.save("whole second")
        with patch.object(self.service, "_clock", return_value=BASE + timedelta(microseconds=1)):
            second, _ = self.save("fractional second")
        self.assertEqual(self.catalog()["items"], [first, second])

    def test_raw_receiver_units_are_not_eligible(self):
        session = self.service.create_session(session_payload())
        self.service.ingest_sweep(measurement(
            session.id, session.streams[1].id, 900, level_unit="s_meter_count",
        ))
        with self.assertRaises(ComparisonIncompatibleError):
            self.service.create_sweep_reference(session.id, {"name": "receiver", "sweep_id": "sweep-900"})
        self.assertEqual(self.service.list_sweep_references(session.id)["items"], [])

    def test_literal_names_and_opaque_ids_are_safe(self):
        sweep_id = " odd/+ ' OR 1=1 -- "
        self.add(id=sweep_id)
        record, _ = self.save("<script>alert('x')</script> ; DROP TABLE sweeps;", sweep_id)
        self.assertEqual(record["sweep_id"], sweep_id)
        self.assertEqual(self.catalog()["items"], [record])
        self.assertEqual(self.service.list_sweeps(self.session.id)[0].id, sweep_id)


class MemoryReferenceTests(ReferenceServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id

    def test_fresh_memory_service_has_no_catalog(self):
        self.add()
        self.save()
        fresh = SessionService()
        self.addCleanup(fresh.close)
        payload = to_primitive(self.session)
        payload.pop("created_at")
        session = fresh.create_session(payload)
        self.assertEqual(fresh.list_sweep_references(session.id)["items"], [])


class EphemeralSQLiteReferenceTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteSessionStore(":memory:")
        self.service = SessionService(store=self.store)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.service.ingest_sweep(measurement(self.session.id, self.session.streams[0].id))
        self.reference, _ = self.service.create_sweep_reference(
            self.session.id, {"name": "Temporary", "sweep_id": "sweep-0"},
        )

    def test_flags_distinguish_sqlite_backend_from_restart_durability(self):
        self.assertFalse(self.store.persistent)
        capabilities = self.service.capabilities()
        self.assertEqual(capabilities["session_storage"], "sqlite")
        self.assertFalse(capabilities["persistent_archive"])
        self.assertFalse(capabilities["features"]["session_recovery"])
        self.assertTrue(capabilities["features"]["named_sweep_references"])
        catalog = self.service.list_sweep_references(self.session.id)
        self.assertEqual(catalog["storage_mode"], "sqlite")
        self.assertFalse(catalog["persistent"])
        self.assertEqual(catalog["items"], [self.reference])

    def test_reopening_ephemeral_sqlite_loses_catalog_sessions_and_measurements(self):
        self.service.close()
        fresh = SessionService(store=SQLiteSessionStore(":memory:"))
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.list_sessions(), [])
        self.assertEqual(fresh.events(), [])
        with self.assertRaises(SessionNotFoundError):
            fresh.list_sweep_references(self.session.id)
        payload = to_primitive(self.session)
        payload.pop("created_at")
        fresh.create_session(payload)
        self.assertEqual(fresh.list_sweep_references(self.session.id)["items"], [])
        self.assertEqual(fresh.list_sweeps(self.session.id), [])

    def test_ephemeral_sqlite_initializes_latest_schema_and_foreign_keys(self):
        connection = self.store._connection
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sweep_references").fetchone()[0], 1)


class SQLiteReferenceTests(ReferenceServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(lambda: self.service.close())
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id

    def test_restart_and_full_archive_allows_catalog_writes_not_measurements(self):
        self.add()
        original, _ = self.save()
        size = self.service.archive_capacity()["database_bytes"]
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=size)
        self.service = SessionService(store=self.store, history_limit=2)
        self.assertEqual(self.service.archive_capacity()["state"], "full")
        self.assertEqual(self.catalog()["items"], [original])
        self.assertEqual(self.save(), (original, False))
        self.assertTrue(self.save("another")[1])
        self.assertEqual(len(self.catalog()["items"]), 2)
        with self.assertRaises(ArchiveCapacityExceededError):
            self.add(1)
        self.assertEqual(len(self.service.list_sweeps(self.session.id)), 1)

    def test_event_failure_rolls_back_row_cursor_history_and_subscribers(self):
        self.add()
        before = (self.service.health(), self.service.events(), self.catalog())
        subscriber_id, subscriber = self.service.subscribe(after=before[0]["last_event_id"])
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        insert = self.store._insert_event
        def fail_after_event(event):
            insert(event)
            raise sqlite3.OperationalError("injected failure after both inserts")
        with patch.object(self.store, "_insert_event", side_effect=fail_after_event):
            with self.assertRaises(sqlite3.OperationalError):
                self.save()
        self.assertEqual((self.service.health(), self.service.events(), self.catalog()), before)
        self.assertEqual(self.store.load_events()[-1].sequence, before[0]["last_event_id"])
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        self.assertTrue(self.save()[1])
        self.assertEqual(subscriber.get_nowait().sequence, before[0]["last_event_id"] + 1)

    def test_schema_has_no_level_copies_and_composite_fk_enforces_ownership(self):
        self.add()
        reference, _ = self.save()
        connection = self.store._connection
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sweep_references)")}
        self.assertEqual(columns, {"id", "session_id", "stream_id", "sweep_id", "name", "created_at",
                                   "started_at", "ended_at", "level_unit", "points"})
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute("UPDATE sweep_references SET stream_id = ? WHERE id = ?",
                               (self.session.streams[1].id, reference["id"]))
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute("UPDATE sweep_references SET session_id = ? WHERE id = ?", ("other", reference["id"]))
        self.assertEqual(self.catalog()["items"], [reference])

    def test_exact_id_lookup_without_archive_scan(self):
        self.add()
        with patch.object(self.store, "list_sweeps", side_effect=AssertionError("no archive scan")):
            self.assertTrue(self.save()[1])


class ReferenceMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        # Build the exact pre-catalog schema using the unchanged v2 DDL.
        with patch.object(SQLiteSessionStore, "_create_reference_schema"):
            service = SessionService(store=SQLiteSessionStore(self.path))
        self.session = create_session(service)
        service.ingest_sweep(measurement(self.session.id, self.session.streams[0].id))
        self.before = (service.list_sessions(), service.list_sweeps(self.session.id), service.events())
        service._store._connection.execute("PRAGMA user_version = 2")
        service.close()

    def test_additive_upgrade_preserves_measurements_events_and_can_create_reference(self):
        service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(service.close)
        self.assertEqual(service._store._connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual((service.list_sessions(), service.list_sweeps(self.session.id), service.events()), self.before)
        self.assertEqual(service.list_sweep_references(self.session.id)["items"], [])
        self.assertTrue(service.create_sweep_reference(self.session.id, {"name": "old", "sweep_id": "sweep-0"})[1])
        self.assertEqual(service._store._connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failure_after_ddl_keeps_version_two_and_no_partial_schema(self):
        original = SQLiteSessionStore._create_reference_schema
        def fail(store):
            original(store)
            raise sqlite3.OperationalError("injected migration failure")
        with patch.object(SQLiteSessionStore, "_create_reference_schema", fail):
            with self.assertRaises(sqlite3.OperationalError):
                SQLiteSessionStore(self.path)
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE name IN ('sweep_references', 'sweeps_reference_owner', 'sweep_references_session_time')").fetchall(), [])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], len(self.before[2]))
        service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(service.close)
        self.assertEqual((service.list_sessions(), service.list_sweeps(self.session.id), service.events()), self.before)

    def test_future_version_is_rejected_before_journal_or_schema_changes(self):
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        before = Path(self.path).read_bytes()
        with self.assertRaises(ValueError):
            SQLiteSessionStore(self.path)
        self.assertEqual(Path(self.path).read_bytes(), before)
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")


class ReferenceHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(path))
        self.session = create_session(service)
        for number in range(3):
            service.ingest_sweep(measurement(
                self.session.id, self.session.streams[0].id, number,
                settings={} if number == 2 else {"rbw_hz": 10000},
            ))
        self.other = create_session(service)
        service.ingest_sweep(measurement(self.other.id, self.other.streams[0].id, 9))
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/sweep-references"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, method="GET", path=None, payload=None, raw=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            body = raw if raw is not None else (None if payload is None else json.dumps(payload))
            connection.request(method, path or self.root, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data.startswith(b"{") else data
        finally:
            connection.close()

    def test_create_list_replay_filter_and_event_at_full_capacity(self):
        self.assertEqual(self.server.session_service.archive_capacity()["state"], "full")
        payload = {"name": "Baseline", "sweep_id": "sweep-0"}
        status, result = self.request("POST", payload=payload)
        self.assertEqual(status, 201)
        self.assertEqual(result["contract_version"], 1)
        record = result["data"]
        before = self.server.session_service.health()["last_event_id"]
        self.assertEqual(self.request("POST", payload=payload), (200, result))
        self.assertEqual(self.server.session_service.health()["last_event_id"], before)
        status, catalog = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(catalog["data"]["items"], [record])
        self.assertTrue(catalog["data"]["persistent"])
        status, filtered = self.request(path=self.root + "?" + urlencode({"stream_id": self.session.streams[1].id}))
        self.assertEqual(status, 200)
        self.assertEqual(filtered["data"]["items"], [])
        status, compared = self.request(path=self.root.replace("sweep-references", "sweep-comparison") + "?" + urlencode({"reference_id": record["sweep_id"], "target_id": "sweep-1"}))
        self.assertEqual(status, 200)
        self.assertEqual(compared["data"]["delta_levels"], [0] * 4)

    def test_invalid_queries_and_post_queries(self):
        for query in ("?x=1", "?limit=100", "?stream_id=", "?stream_id=%20", "?stream_id",
                      "?stream_id=x&stream_id=x", "?stream_id=x&", "?stream_id=%FF",
                      "?stream_id=%ZZ", "?stream_id=%", "?stream_id=unknown"):
            with self.subTest(query=query):
                status, result = self.request(path=self.root + query)
                self.assertEqual(status, 400)
                self.assertEqual(result["error"]["code"], "invalid_request")
        for query in ("?stream_id=x", "?x=1", "?stream_id=%FF"):
            with self.subTest(query=query):
                self.assertEqual(self.request("POST", self.root + query, {"name": "x", "sweep_id": "sweep-0"})[0], 400)

    def test_strict_body_and_names(self):
        for raw in (b'{', b'[]', b'null', b'"x"', b'{}', b'{"name":"x"}',
                    b'{"name":"x","sweep_id":"sweep-0","extra":1}',
                    b'{"name":"x","name":"y","sweep_id":"sweep-0"}',
                    b'{"name":"x","sweep_id":"sweep-0","sweep_id":"sweep-1"}',
                    b'{"name":"\xff","sweep_id":"sweep-0"}'):
            with self.subTest(raw=raw):
                status, result = self.request("POST", raw=raw)
                self.assertEqual(status, 400)
                self.assertEqual(result["error"]["code"], "invalid_request")
        for name in (None, "", "x\n", "x" * 81, "x\u200d", "x\ue000", "x\ud800"):
            with self.subTest(name=repr(name)):
                self.assertEqual(self.request("POST", payload={"name": name, "sweep_id": "sweep-0"})[0], 400)
        duplicate = b'{"name":"x","name":"y","sweep_id":"sweep-0"}'
        self.assertEqual(self.request("POST", self.root + "/", raw=duplicate)[0], 400)
        invalid_key = b'{"\\ud800":1,"\\ud800":2}'
        self.assertEqual(self.request("POST", raw=invalid_key)[0], 400)
        self.assertEqual(self.request()[1]["data"]["items"], [])

    def test_error_classification_and_no_edit_delete(self):
        self.request("POST", payload={"name": "Baseline", "sweep_id": "sweep-0"})
        for sweep_id, expected, code in (
            ("sweep-1", 409, "reference_name_conflict"),
            ("sweep-2", 409, "comparison_incompatible"),
            ("missing", 404, "sweep_not_found"),
            ("sweep-9", 404, "sweep_not_found"),
        ):
            with self.subTest(sweep_id=sweep_id):
                status, result = self.request("POST", payload={"name": "Baseline", "sweep_id": sweep_id})
                self.assertEqual(status, expected)
                self.assertEqual(result["error"]["code"], code)
        for method in ("GET", "POST"):
            status, result = self.request(method, "/api/v1/sessions/missing/sweep-references", {"name": "x", "sweep_id": "sweep-0"})
            self.assertEqual(status, 404)
            self.assertEqual(result["error"]["code"], "session_not_found")
        for method in ("PUT", "PATCH", "DELETE"):
            self.assertIn(self.request(method)[0], (404, 405, 501))
        self.assertEqual(self.request("POST", self.root + "/arbitrary", {})[0], 404)

    def test_cap_error_and_replay_at_cap(self):
        service = self.server.session_service
        for number in range(MAX_SWEEP_REFERENCES):
            service.create_sweep_reference(self.session.id, {"name": str(number), "sweep_id": "sweep-0"})
        status, result = self.request("POST", payload={"name": "extra", "sweep_id": "sweep-0"})
        self.assertEqual(status, 409)
        self.assertEqual(result["error"]["code"], "reference_limit_reached")
        self.assertEqual(self.request("POST", payload={"name": "0", "sweep_id": "sweep-0"})[0], 200)


if __name__ == "__main__":
    unittest.main()
