"""Regression checks for archive identity, transactions and chronological queries."""

from __future__ import annotations

import copy
import queue
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from radioinspector.contracts import MonitoringSession, SessionState, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import BASE, session_payload


class ArchiveRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = str(Path(self.temporary.name) / "sessions.sqlite")

    def service(self, persistent=True):
        service = SessionService(
            store=SQLiteSessionStore(self.path) if persistent else None,
            clock=lambda: BASE,
        )
        self.addCleanup(service.close)
        return service

    @staticmethod
    def payload(session_id, stream_id="shared-stream"):
        payload = session_payload()
        payload["id"] = session_id
        payload["streams"][0]["id"] = stream_id
        return payload

    def test_stream_collision_rejected_without_changing_original_session(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                service = self.service(persistent)
                original = service.create_session(self.payload("a"))
                before = to_primitive(original)
                conflicting = self.payload("b")
                conflicting["streams"][0]["device"] = "replacement-device"
                with self.assertRaisesRegex(ValueError, "[Пп]оток"):
                    service.create_session(conflicting)
                self.assertEqual([s.id for s in service.list_sessions()], ["a"])
                self.assertEqual(to_primitive(service.get_session("a")), before)
                self.assertEqual(service.health()["last_event_id"], 1)
                if persistent:
                    restored = self.service()
                    self.assertEqual([s.id for s in restored.list_sessions()], ["a"])
                    self.assertEqual(to_primitive(restored.get_session("a")), before)

    def test_store_collision_rolls_back_session_and_earlier_stream_writes(self):
        store = SQLiteSessionStore(self.path)
        self.addCleanup(store.close)
        original = MonitoringSession.from_dict(self.payload("a"))
        store.save_session(original)
        incoming = self.payload("b", "new-stream")
        incoming["streams"][1]["id"] = "shared-stream"
        incoming["streams"][1]["device"] = "replacement-device"
        with self.assertRaisesRegex(ValueError, "[Пп]оток"):
            store.save_session(MonitoringSession.from_dict(incoming))
        self.assertEqual([to_primitive(s) for s in store.load_sessions()], [to_primitive(original)])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM streams").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_lifecycle_updates_preserve_existing_measurements_and_metadata(self):
        service = self.service()
        session = service.create_session(self.payload("a"))
        sweep = service.ingest_sweep(sweep_payload("a", session.streams[0].id))
        observation = service.ingest_observation(observation_payload("a", session.streams[1].id))
        service.start_session("a", at=BASE)
        service.stop_session("a", at=BASE + timedelta(seconds=1))
        restored = self.service()
        self.assertEqual(to_primitive(restored.list_sweeps("a")), [to_primitive(sweep)])
        self.assertEqual(to_primitive(restored.list_observations("a")), [to_primitive(observation)])
        self.assertEqual(to_primitive(restored.get_session("a").streams), to_primitive(session.streams))
        self.assertEqual(restored.get_session("a").metadata, session.metadata)
        self.assertEqual([e.event_type for e in restored.events()], [
            "session.created", "sweep.received", "observation.received",
            "session.started", "session.stopped",
        ])

    def test_failed_event_rolls_back_every_state_or_measurement_change(self):
        for action, event_type in (
            ("create", "session.created"),
            ("start", "session.started"),
            ("stop", "session.stopped"),
            ("sweep", "sweep.received"),
            ("observation", "observation.received"),
        ):
            with self.subTest(action=action):
                path = str(Path(self.temporary.name) / (action + ".sqlite"))
                service = SessionService(store=SQLiteSessionStore(path), clock=lambda: BASE)
                self.addCleanup(service.close)
                if action != "create":
                    session = service.create_session(self.payload("a"))
                if action == "stop":
                    service.start_session("a", at=BASE)
                before_sessions = to_primitive(service.list_sessions())
                before_events = to_primitive(service.events())
                before_health = service.health()
                _, subscriber = service.subscribe(after=before_health["last_event_id"])
                with sqlite3.connect(path) as connection:
                    connection.execute(
                        "CREATE TRIGGER reject_event BEFORE INSERT ON events "
                        f"WHEN NEW.event_type = '{event_type}' "
                        "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"
                    )
                def perform():
                    if action == "create":
                        return service.create_session(self.payload("a"))
                    if action == "start":
                        return service.start_session("a", at=BASE)
                    if action == "stop":
                        return service.stop_session("a", at=BASE + timedelta(seconds=1))
                    if action == "sweep":
                        return service.ingest_sweep(sweep_payload("a", session.streams[0].id))
                    return service.ingest_observation(observation_payload("a", session.streams[1].id))
                with self.assertRaises((sqlite3.IntegrityError, ValueError)):
                    perform()
                self.assertEqual(to_primitive(service.list_sessions()), before_sessions)
                self.assertEqual(to_primitive(service.events()), before_events)
                self.assertEqual(service.health(), before_health)
                with self.assertRaises(queue.Empty):
                    subscriber.get_nowait()
                restored = SessionService(store=SQLiteSessionStore(path))
                try:
                    self.assertEqual(to_primitive(restored.list_sessions()), before_sessions)
                    self.assertEqual(to_primitive(restored.events()), before_events)
                    if action != "create":
                        self.assertEqual(restored.list_sweeps("a"), [])
                        self.assertEqual(restored.list_observations("a"), [])
                finally:
                    restored.close()
                with sqlite3.connect(path) as connection:
                    connection.execute("DROP TRIGGER reject_event")
                perform()
                event = subscriber.get_nowait()
                self.assertEqual(event.sequence, before_health["last_event_id"] + 1)
                self.assertEqual(event.event_type, event_type)
                with sqlite3.connect(path) as connection:
                    count = connection.execute("SELECT count(*) FROM events").fetchone()[0]
                    self.assertEqual(count, event.sequence)
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_process_exit_during_event_insert_does_not_commit_partial_change(self):
        child = r"""
import os
import sys
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_archive_regressions import ArchiveRegressionTests
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import BASE
store = SQLiteSessionStore(sys.argv[1])
service = SessionService(store=store, clock=lambda: BASE)
store._connection.create_function("crash", 0, lambda: os._exit(73))
store._connection.execute(
    "CREATE TEMP TRIGGER crash_at_event BEFORE INSERT ON events "
    "BEGIN SELECT crash(); END"
)
action = sys.argv[2]
if action == "create":
    service.create_session(ArchiveRegressionTests.payload("a"))
elif action == "start":
    service.start_session("a", at=BASE)
elif action == "stop":
    service.stop_session("a", at=BASE)
else:
    session = service.get_session("a")
    if action == "sweep":
        service.ingest_sweep(sweep_payload("a", session.streams[0].id))
    else:
        service.ingest_observation(observation_payload("a", session.streams[1].id))
raise AssertionError("The event trigger did not interrupt the process")
"""
        for action in ("create", "start", "stop", "sweep", "observation"):
            with self.subTest(action=action):
                path = str(Path(self.temporary.name) / ("crash-" + action + ".sqlite"))
                service = SessionService(store=SQLiteSessionStore(path), clock=lambda: BASE)
                if action != "create":
                    service.create_session(self.payload("a"))
                if action == "stop":
                    service.start_session("a", at=BASE)
                before_sessions = to_primitive(service.list_sessions())
                before_events = to_primitive(service.events())
                service.close()
                result = subprocess.run(
                    [sys.executable, "-c", child, path, action],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 73, result.stderr)
                restored = SessionService(store=SQLiteSessionStore(path))
                try:
                    self.assertEqual(to_primitive(restored.list_sessions()), before_sessions)
                    self.assertEqual(to_primitive(restored.events()), before_events)
                    if action != "create":
                        self.assertEqual(restored.list_sweeps("a"), [])
                        self.assertEqual(restored.list_observations("a"), [])
                finally:
                    restored.close()

    def test_measurement_order_matches_time_not_arrival_or_string_precision(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                service = self.service(persistent)
                session = service.create_session(self.payload("a"))
                for kind, factory, index in (
                    ("sweep", sweep_payload, 0),
                    ("observation", observation_payload, 1),
                ):
                    # Reverse arrival and IDs ensure only timestamp ordering can pass.
                    for suffix, fraction in (("a", 500000), ("b", 1), ("z", 0)):
                        payload = factory("a", session.streams[index].id)
                        payload["id"] = kind + "-" + suffix
                        moment = BASE + timedelta(microseconds=fraction)
                        payload["interval"] = {"started_at": to_primitive(moment), "ended_at": to_primitive(moment)}
                        getattr(service, "ingest_" + kind)(payload)
                    listing = getattr(service, "list_" + ("sweeps" if kind == "sweep" else "observations"))
                    expected = [kind + "-z", kind + "-b", kind + "-a"]
                    self.assertEqual([s.id for s in listing("a")], expected)
                    self.assertEqual([s.id for s in listing("a", limit=1)], expected[-1:])
                    self.assertEqual([s.id for s in listing("a", stream_id=session.streams[index].id, limit=2)], expected[-2:])
                if persistent:
                    # Keep legacy whole-second encodings to test pre-fix archives too.
                    with sqlite3.connect(self.path) as connection:
                        for table in ("sweeps", "observations"):
                            connection.execute(f"UPDATE {table} SET started_at = '2026-11-04T20:00:00Z' WHERE id LIKE '%-z'")
                    restored = self.service()
                    self.assertEqual([s.id for s in restored.list_sweeps("a")], ["sweep-z", "sweep-b", "sweep-a"])
                    self.assertEqual([s.id for s in restored.list_observations("a", limit=1)], ["observation-a"])

    def test_equal_measurement_times_use_id_before_applying_limit(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                service = self.service(persistent)
                session = service.create_session(self.payload("a"))
                for kind, factory, index, plural in (
                    ("sweep", sweep_payload, 0, "sweeps"),
                    ("observation", observation_payload, 1, "observations"),
                ):
                    for suffix in ("z", "a"):
                        payload = factory("a", session.streams[index].id)
                        payload["id"] = kind + "-" + suffix
                        getattr(service, "ingest_" + kind)(payload)
                    if persistent:
                        with sqlite3.connect(self.path) as connection:
                            connection.execute(
                                f"UPDATE {plural} SET started_at = '2026-11-04T20:00:00Z' WHERE id = ?",
                                (kind + "-a",),
                            )
                    listing = getattr(service, "list_" + plural)
                    self.assertEqual([s.id for s in listing("a")], [kind + "-a", kind + "-z"])
                    self.assertEqual([s.id for s in listing("a", limit=1)], [kind + "-z"])

    def test_creation_rejects_lifecycle_injection_and_does_not_mutate_payload(self):
        service = self.service()
        for index, (field, value) in enumerate((
            ("state", "running"), ("state", "stopped"), ("state", "failed"),
            ("created_at", "2020-01-01T00:00:00Z"),
            ("started_at", "2026-11-04T20:00:00Z"),
            ("ended_at", "2026-11-04T20:00:00Z"),
        )):
            with self.subTest(field=field, value=value):
                rejected_service = SessionService(store=SQLiteSessionStore(str(Path(self.temporary.name) / f"creation-{index}.sqlite")))
                self.addCleanup(rejected_service.close)
                payload = self.payload("a")
                payload[field] = value
                before = copy.deepcopy(payload)
                with self.assertRaises(ValueError):
                    rejected_service.create_session(payload)
                self.assertEqual(payload, before)
                self.assertEqual(rejected_service.list_sessions(), [])
                self.assertEqual(rejected_service.events(), [])
        payload = self.payload("a")
        payload.update(state="draft", started_at=None, ended_at=None)
        session = service.create_session(payload)
        self.assertEqual(session.state, SessionState.DRAFT)
        self.assertEqual(session.created_at, BASE)
        self.assertIsNone(session.started_at)
        self.assertIsNone(session.ended_at)
        service.start_session("a", at=BASE)
        restored = self.service()
        self.assertEqual(restored.get_session("a").state, SessionState.RUNNING)
