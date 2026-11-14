"""Synthetic archive context: bounded ordering, compatibility, snapshots and HTTP."""

from __future__ import annotations

import copy
import http.client
import json
import queue
import random
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from radioinspector.contracts import Sweep, to_primitive
from radioinspector.session_service import SessionNotFoundError, SessionService, SweepNotFoundError
from radioinspector.session_store import SCHEMA_VERSION, SQLiteSessionStore
from radioinspector.sweep_comparison import MAX_COMPARISON_POINTS, validate_sweep_compatibility
from radioinspector.sweep_context import (
    MAX_CONTEXT_REQUEST_BYTES, MAX_CONTEXT_TOTAL_POINTS, MAX_FREQUENCY_HZ,
    ContextIncompatibleError, calculate_sweep_context, select_context_neighbours,
    validate_context_request,
)
from radioinspector.web.server import create_server
from tests.test_platform import BASE
from tests.test_sweep_comparison import create_session, measurement


def request_body(sweep_id="sweep-0", before=0, after=0):
    return {"sweep_id": sweep_id, "before": before, "after": after}


def interval(microseconds, *, duration=2_000_000):
    return {"started_at": to_primitive(BASE + timedelta(microseconds=microseconds)),
            "ended_at": to_primitive(BASE + timedelta(microseconds=microseconds + duration))}


class ContextContractTests(unittest.TestCase):
    def test_request_exact_shape_integer_bounds_utf8_codepoints(self):
        invalid = [None, [], True, {}, dict(request_body(), extra=1), {"sweep_id": "x"}]
        for field in request_body():
            payload = request_body()
            del payload[field]
            invalid.append(payload)
        invalid += [request_body(value) for value in (None, True, 1, {}, [], "", " \t\n", "x" * 129, "😀" * 129, "\ud800")]
        for field in ("before", "after"):
            invalid += [dict(request_body(), **{field: value})
                        for value in (None, True, False, -1, 17, 1.0, "1", [], {}, float("nan"), float("inf"), 10**1000)]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_context_request(payload)
        for sweep_id in (" exact/+ sweep ", "😀" * 128, "é", "e\u0301", "\x00"):
            for before, after in ((0, 0), (16, 16), (0, 16), (16, 0)):
                self.assertEqual(validate_context_request(request_body(sweep_id, before, after)), (sweep_id, before, after))

    def test_shared_gate_bad_sources_safe_hz_and_detached_data(self):
        anchor = Sweep.from_dict(measurement(settings={"rbw_hz": 1, "sequence": 1, "extension": {"values": [1]}}))
        following = copy.deepcopy(anchor)
        following.id = "next"
        following.settings["sequence"] = None
        with patch("radioinspector.sweep_context.validate_sweep_compatibility", wraps=validate_sweep_compatibility) as gate:
            result = calculate_sweep_context("session", anchor, [], [following], before=0, after=1)
            self.assertEqual(gate.call_count, 2)
        original = copy.deepcopy(anchor)
        result["items"][0]["levels"][0] = 1
        result["items"][0]["settings"]["extension"]["values"].append(2)
        result["items"][0]["interval"]["started_at"] = "changed"
        result["frequencies_hz"][0] = 999
        self.assertEqual(anchor, original)
        self.assertEqual(result["items"][1]["settings"]["extension"]["values"], [1])
        for changes in ({"session_id": "other"}, {"stream_id": "other"}, {"level_unit": "dB"},
                        {"settings": {}}, {"settings": {"sequence": 1}}, {"settings": {"rbw_hz": None}},
                        {"settings": {"rbw_hz": 2}}, {"settings": {"x": object()}},
                        {"settings": {"x": float("inf")}}, {"settings": {"x": "\ud800"}},
                        {"levels": ()}, {"levels": (float("nan"),) * 4},
                        {"frequencies_hz": (1, 2, 3, MAX_FREQUENCY_HZ + 1)},
                        {"frequencies_hz": (True,) * 4}, {"frequencies_hz": (-1,) * 4},
                        {"frequencies_hz": (1.0,) * 4}, {"id": "\ud800"}):
            bad = copy.deepcopy(anchor)
            for field, value in changes.items():
                setattr(bad, field, value)
            with self.subTest(changes=changes), self.assertRaises(ContextIncompatibleError):
                calculate_sweep_context("session", anchor, [], [bad], before=0, after=1)
        for frequency in (0, MAX_FREQUENCY_HZ):
            source = Sweep.from_dict(measurement(frequencies_hz=[frequency], levels=[sys.float_info.max]))
            result = calculate_sweep_context("session", source, [], [], before=0, after=0)
            self.assertEqual((result["points"], result["frequencies_hz"], result["items"][0]["levels"]),
                             (1, [frequency], [sys.float_info.max]))

    def test_per_sweep_and_total_point_limits_without_truncation(self):
        points = MAX_COMPARISON_POINTS
        anchor = Sweep.from_dict(measurement(frequencies_hz=list(range(points)), levels=[-50] * points))
        following = [copy.deepcopy(anchor) for _ in range(8)]
        for index, sweep in enumerate(following):
            sweep.id = f"next-{index}"
        result = calculate_sweep_context("session", anchor, [], following[:7], before=0, after=7)
        self.assertEqual(result["points"] * len(result["items"]), MAX_CONTEXT_TOTAL_POINTS)
        self.assertTrue(all(len(item["levels"]) == points for item in result["items"]))
        with self.assertRaises(ContextIncompatibleError):
            calculate_sweep_context("session", anchor, [], following, before=0, after=8)
        anchor.frequencies_hz = tuple(range(points + 1))
        anchor.levels = (-50,) * (points + 1)
        with self.assertRaises(ContextIncompatibleError):
            calculate_sweep_context("session", anchor, [], [], before=0, after=0)

    def test_memory_selection_one_pass_bounded_and_no_deepcopy(self):
        anchor = Sweep.from_dict(measurement(number=100))
        class OnePass:
            def __init__(self):
                self.used = False
            def __iter__(self):
                if self.used:
                    raise AssertionError("archive iterated twice")
                self.used = True
                for index in reversed(range(200)):
                    yield Sweep.from_dict(measurement(number=index))
        with patch("radioinspector.sweep_context.copy.deepcopy", side_effect=AssertionError("no archive copies")):
            preceding, following = select_context_neighbours(anchor, OnePass(), before=16, after=16)
        self.assertEqual([s.id for s in preceding], [f"sweep-{i}" for i in range(84, 100)])
        self.assertEqual([s.id for s in following], [f"sweep-{i}" for i in range(101, 117)])


class ContextServiceMixin:
    def add(self, number=0, **changes):
        return self.service.ingest_sweep(measurement(self.session.id, self.stream_id, number, **changes))

    def context(self, sweep_id="sweep-0", before=0, after=0):
        return self.service.sweep_context(self.session.id, request_body(sweep_id, before, after))

    def snapshot(self):
        return self.service.health(), self.service.events(), self.service.list_sweeps(self.session.id, limit=None)

    def test_exact_contract_missing_counts_zero_and_nearest_selection(self):
        for number in (4, 0, 3, 1, 2):
            self.add(number)
        result = self.context("sweep-2", 1, 1)
        self.assertEqual(set(result), {"session_id", "stream_id", "anchor_id", "order", "requested", "available",
                                       "missing", "complete", "frequencies_hz", "level_unit", "points", "items"})
        self.assertEqual((result["anchor_id"], result["order"], result["level_unit"], result["points"]),
                         ("sweep-2", "started_at_then_id", "dBm", 4))
        self.assertEqual(result["requested"], {"before": 1, "after": 1})
        self.assertEqual(result["available"], result["requested"])
        self.assertEqual(result["missing"], {"before": 0, "after": 0})
        self.assertTrue(result["complete"])
        self.assertEqual([(s["id"], s["offset"]) for s in result["items"]], [("sweep-1", -1), ("sweep-2", 0), ("sweep-3", 1)])
        self.assertEqual(set(result["items"][0]), {"offset", "id", "interval", "levels", "level_unit", "settings"})
        for sweep_id, preceding, following in (("sweep-0", 0, 4), ("sweep-2", 2, 2), ("sweep-4", 4, 0)):
            result = self.context(sweep_id, 16, 16)
            self.assertEqual(result["available"], {"before": preceding, "after": following})
            self.assertEqual(result["missing"], {"before": 16 - preceding, "after": 16 - following})
            self.assertFalse(result["complete"])
            zero = self.context(sweep_id)
            self.assertEqual(zero["requested"], zero["available"])
            self.assertEqual(zero["available"], zero["missing"])
            self.assertTrue(zero["complete"])
            self.assertEqual([(s["id"], s["offset"]) for s in zero["items"]], [(sweep_id, 0)])

    def test_out_of_order_ties_utc_microseconds_and_overlapping_intervals(self):
        rows = [("z", 1), ("😀", 0), ("e\u0301", 0), ("a", 0), ("é", 0), ("later", 2), ("old", -1)]
        for index, (identity, microseconds) in enumerate(rows):
            self.add(index, id=identity, interval=interval(microseconds))
        # A different stream in the same session and a different session do not
        # count even when closer in time or incompatible.
        self.service.ingest_sweep(measurement(self.session.id, self.session.streams[1].id, 40, id="other-stream",
                                               interval=interval(0), settings={}))
        other = create_session(self.service)
        self.service.ingest_sweep(measurement(other.id, other.streams[0].id, 41, id="other-session", interval=interval(0)))
        result = self.context("é", 16, 16)
        expected = ["old", "a", "e\u0301", "é", "😀", "z", "later"]
        self.assertEqual([item["id"] for item in result["items"]], expected)
        self.assertEqual([item["offset"] for item in result["items"]], list(range(-3, 4)))
        self.assertEqual(result["available"], {"before": 3, "after": 3})
        self.assertLess(result["items"][1]["interval"]["started_at"], result["items"][0]["interval"]["ended_at"])

    def test_old_anchor_beyond_recent_limits_without_recent_list_or_events(self):
        for base in range(0, 1100, 100):
            self.service.ingest_sweeps([measurement(self.session.id, self.stream_id, i) for i in range(base, base + 100)])
        self.assertNotIn("sweep-0", [item.id for item in self.service.list_sweeps(self.session.id, limit=1000)])
        with patch.object(self.service, "list_sweeps", side_effect=AssertionError("not recent list")), \
                patch.object(self.service, "events", side_effect=AssertionError("not events")):
            result = self.context("sweep-0", 16, 16)
        self.assertEqual([s["id"] for s in result["items"]], [f"sweep-{i}" for i in range(17)])
        self.assertEqual(result["missing"], {"before": 16, "after": 0})

    def test_incompatible_neighbour_is_not_skipped_and_zero_does_not_inspect_it(self):
        self.add()
        self.add(1, settings={"rbw_hz": 999})
        self.add(2)
        self.assertTrue(self.context()["complete"])
        for sweep_id, before, after in (("sweep-0", 0, 1), ("sweep-0", 0, 2), ("sweep-2", 1, 0), ("sweep-2", 2, 0)):
            with self.subTest(sweep_id=sweep_id), self.assertRaises(ContextIncompatibleError):
                self.context(sweep_id, before, after)

    def test_settings_unknown_unit_grid_or_oversized_neighbour_reject_whole_window(self):
        self.add()
        for index, changes in enumerate(({"settings": {}}, {"settings": {"sequence": 1}},
                                         {"settings": {"rbw_hz": None}},
                                         {"frequencies_hz": [100, 200, 300, 401]},
                                         {"frequencies_hz": list(range(16385)), "levels": [-50] * 16385}), 1):
            self.add(index, **changes)
            with self.assertRaises(ContextIncompatibleError):
                self.context(f"sweep-{index}", 1, 0)

    def test_success_failure_detached_response_no_events_and_late_import(self):
        self.add(settings={"rbw_hz": 100, "sequence": 0, "extension": {"values": [1]}})
        self.add(2, settings={"rbw_hz": 100, "sequence": 2, "extension": {"values": [1]}})
        self.add(3, settings={})
        before = self.snapshot()
        subscriber_id, subscriber = self.service.subscribe(after=before[0]["last_event_id"])
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        result = self.context("sweep-0", 0, 1)
        frozen = copy.deepcopy(result)
        result["items"][0]["settings"]["extension"]["values"].append(2)
        result["items"][0]["levels"][0] = 999
        result["frequencies_hz"][0] = 999
        with self.assertRaises(ContextIncompatibleError):
            self.context("sweep-0", 0, 3)
        with self.assertRaises(ValueError):
            self.context(before=True)
        with self.assertRaises(SweepNotFoundError):
            self.context("missing")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.context("sweep-0", 0, 1), frozen)
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        self.add(1, settings={"rbw_hz": 100, "sequence": 1, "extension": {"values": [1]}})
        self.assertEqual(self.service.health()["last_event_id"], before[0]["last_event_id"] + 1)
        self.assertEqual([item["id"] for item in self.context("sweep-0", 0, 1)["items"]], ["sweep-0", "sweep-1"])
        self.assertEqual([item["id"] for item in frozen["items"]], ["sweep-0", "sweep-2"])

    def test_exact_non_normalized_ids_missing_foreign_session_and_capability(self):
        for index, sweep_id in enumerate((" exact/+ sweep ", "😀" * 128, "é", "e\u0301")):
            self.add(index, id=sweep_id)
            self.assertEqual(self.context(sweep_id)["anchor_id"], sweep_id)
        other = create_session(self.service)
        self.service.ingest_sweep(measurement(other.id, other.streams[0].id, 40))
        for sweep_id in ("missing", "sweep-40", "exact/+ sweep"):
            with self.assertRaises(SweepNotFoundError):
                self.context(sweep_id)
        with self.assertRaises(SessionNotFoundError):
            self.service.sweep_context("missing", request_body())
        self.assertTrue(self.service.capabilities()["features"]["sweep_context"])

    def test_long_neighbour_id_rejected_only_if_selected(self):
        self.add()
        self.add(1)
        self.add(2, id="x" * 129)
        self.assertEqual(len(self.context("sweep-0", 0, 1)["items"]), 2)
        with self.assertRaises(ContextIncompatibleError):
            self.context("sweep-0", 0, 2)

    def test_total_point_cap_is_whole_window_not_each_side(self):
        for number in range(9):
            self.add(number, frequencies_hz=list(range(16384)), levels=[-50] * 16384)
        result = self.context("sweep-4", 4, 3)
        self.assertEqual(len(result["items"]) * result["points"], MAX_CONTEXT_TOTAL_POINTS)
        with self.assertRaises(ContextIncompatibleError):
            self.context("sweep-4", 4, 4)


class MemoryContextTests(ContextServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


    def test_anchor_only_does_not_inspect_unrelated_malformed_id(self):
        self.add()
        # Memory ingest historically permits this ID; it cannot be ordered as
        # UTF-8, but an anchor-only request must never inspect the other row.
        self.add(1, id="\ud800")
        self.assertEqual([item["id"] for item in self.context()["items"]], ["sweep-0"])
        anchor = self.service._get_sweep_locked(self.session.id, "sweep-0")
        class NoIteration:
            def __iter__(self):
                raise AssertionError("0/0 must not scan the archive")
        self.assertEqual(select_context_neighbours(anchor, NoIteration(), before=0, after=0), ([], []))


class SQLiteMemoryContextTests(ContextServiceMixin, unittest.TestCase):
    def setUp(self):
        self.store = SQLiteSessionStore(":memory:")
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteContextTests(ContextServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(lambda: self.service.close())
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id

    def test_bounded_sql_no_writes_no_schema_migration_no_unbounded_archive_fetch(self):
        for number in range(20):
            self.add(number)
        connection = self.store._connection
        before = connection.total_changes, list(connection.iterdump())
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            with patch.object(self.store, "list_sweeps", side_effect=AssertionError("no entire archive")), \
                    patch.object(self.store, "_sweep_from_row", wraps=self.store._sweep_from_row) as decoder:
                result = self.context("sweep-10", 2, 3)
                self.assertEqual(decoder.call_count, 6)
        finally:
            connection.set_trace_callback(None)
        self.assertEqual((connection.total_changes, list(connection.iterdump())), before)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        # The query itself migrates nothing: version above is the one the open
        # store already had, whatever the current additive schema version is.
        self.assertGreaterEqual(SCHEMA_VERSION, 3)
        reads = [statement for statement in statements if statement.startswith("SELECT")]
        self.assertEqual(len(reads), 3)
        self.assertIn("WHERE id = ", reads[0])
        self.assertIn("LIMIT 2", reads[1])
        self.assertIn("LIMIT 3", reads[2])
        self.assertIn("context_utf8(id)", reads[1])
        self.assertEqual((statements[0], statements[-1]), ("BEGIN", "COMMIT"))
        self.assertEqual([item["offset"] for item in result["items"]], [-2, -1, 0, 1, 2, 3])

    def test_legacy_zero_fraction_offset_timezone_and_microseconds(self):
        for number in range(4):
            self.add(number, interval=interval(number))
        with self.store._connection:
            self.store._connection.execute("UPDATE sweeps SET started_at = ? WHERE id = ?", (to_primitive(BASE), "sweep-0"))
            utc = BASE + timedelta(microseconds=1)
            offset = utc.astimezone(timezone(timedelta(hours=3))).isoformat()
            self.store._connection.execute("UPDATE sweeps SET started_at = ? WHERE id = ?", (offset, "sweep-1"))
        result = self.context("sweep-1", 16, 16)
        self.assertEqual([s["id"] for s in result["items"]], ["sweep-0", "sweep-1", "sweep-2", "sweep-3"])
        self.assertEqual(result["items"][1]["interval"]["started_at"], to_primitive(utc))

    def test_read_snapshot_excludes_concurrent_other_connection_import(self):
        self.add()
        self.add(2)
        other = SQLiteSessionStore(self.path)
        self.addCleanup(other.close)
        inserted = []
        def on_statement(statement):
            if "LIMIT" in statement and "FROM sweeps" in statement and not inserted:
                inserted.append(True)
                other.record_sweep(Sweep.from_dict(measurement(self.session.id, self.stream_id, 1)))
        self.store._connection.set_trace_callback(on_statement)
        try:
            first = self.context("sweep-0", 1, 1)
        finally:
            self.store._connection.set_trace_callback(None)
        self.assertEqual(inserted, [True])
        self.assertEqual([s["id"] for s in first["items"]], ["sweep-0", "sweep-2"])
        self.assertEqual([s["id"] for s in self.context("sweep-0", 1, 1)["items"]], ["sweep-0", "sweep-1"])

    def test_corrupt_selected_source_rejects_whole_window_and_rolls_back_read(self):
        self.add()
        self.add(1)
        for column, value in (("levels_json", "[]"), ("levels_json", '["-50",-50,-50,-50]'),
                              ("levels_json", '[true,-50,-50,-50]'), ("levels_json", '{"1":1,"2":2,"3":3,"4":4}'),
                              ("level_unit", " dBm "), ("settings_json", "[]"),
                              ("frequencies_json", "[true]"), ("started_at", "no-time")):
            original = self.store._connection.execute(f"SELECT {column} FROM sweeps WHERE id='sweep-1'").fetchone()[0]
            with self.store._connection:
                self.store._connection.execute(f"UPDATE sweeps SET {column}=? WHERE id='sweep-1'", (value,))
            before = list(self.store._connection.iterdump())
            with self.subTest(column=column), self.assertRaises(ContextIncompatibleError):
                self.context("sweep-0", 0, 1)
            self.assertFalse(self.store._connection.in_transaction)
            self.assertEqual(list(self.store._connection.iterdump()), before)
            with self.store._connection:
                self.store._connection.execute(f"UPDATE sweeps SET {column}=? WHERE id='sweep-1'", (original,))

    def test_restart_full_capacity_keeps_old_anchor_and_window_readable(self):
        self.test_old_anchor_beyond_recent_limits_without_recent_list_or_events()
        expected = self.context("sweep-0", 16, 16)
        size = self.service.archive_capacity()["database_bytes"]
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=size)
        self.service = SessionService(store=self.store, history_limit=2)
        self.assertEqual(self.service.archive_capacity()["state"], "full")
        before = list(self.store._connection.iterdump())
        self.assertEqual(self.context("sweep-0", 16, 16), expected)
        self.assertEqual(list(self.store._connection.iterdump()), before)


class ContextEquivalenceTests(unittest.TestCase):
    def test_utf16_archive_uses_utf8_binary_tie_breaking_without_migration(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "utf16.sqlite")
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA encoding = 'UTF-16'")
        connection.execute("CREATE TABLE fixture (id INTEGER)")
        connection.commit()
        connection.close()
        store = SQLiteSessionStore(path)
        self.assertIn("UTF-16", store._connection.execute("PRAGMA encoding").fetchone()[0])
        service = SessionService(store=store)
        self.addCleanup(service.close)
        session = create_session(service)
        identities = ["\U00010000", "\ue000", "z", "é", "e\u0301", "😀"]
        for number, identity in enumerate(identities):
            service.ingest_sweep(measurement(session.id, session.streams[0].id, number,
                                             id=identity, interval=interval(0)))
        before = list(store._connection.iterdump())
        for identity in identities:
            result = service.sweep_context(session.id, request_body(identity, 16, 16))
            self.assertEqual([item["id"] for item in result["items"]], sorted(identities, key=lambda value: value.encode("utf-8")))
        self.assertEqual(list(store._connection.iterdump()), before)

    def test_memory_sqlite_outputs_equal_full_sort_reference(self):
        memory = SessionService()
        self.addCleanup(memory.close)
        session = create_session(memory)
        store = SQLiteSessionStore(":memory:")
        store.save_session(session)
        sqlite = SessionService(store=store)
        self.addCleanup(sqlite.close)
        rng = random.Random(516)
        sources = [measurement(session.id, session.streams[0].id, number,
                               id=f"{prefix}-{number}", interval=interval(rng.randrange(9)))
                   for number, prefix in enumerate((["é", "e\u0301", "😀", "a", "\U00010000", "\ue000"] * 12))]
        rng.shuffle(sources)
        for service in (memory, sqlite):
            service.ingest_sweeps(sources)
        ordered = sorted(sources, key=lambda source: (Sweep.from_dict(source).interval.started_at, source["id"].encode("utf-8")))
        for _ in range(80):
            position = rng.randrange(len(ordered))
            before, after = rng.randrange(17), rng.randrange(17)
            request = request_body(ordered[position]["id"], before, after)
            first, second = [service.sweep_context(session.id, request) for service in (memory, sqlite)]
            self.assertEqual(first, second)
            self.assertEqual([item["id"] for item in first["items"]],
                             [source["id"] for source in ordered[max(0, position - before):position + after + 1]])
            self.assertEqual(first["missing"], {"before": max(0, before - position),
                                                "after": max(0, position + after + 1 - len(ordered))})


def seed_context_http_archive(service):
    """Build the original private HTTP dataset through the public ingest API."""
    session = create_session(service)
    stream = session.streams[0].id
    service.ingest_sweeps([
        *(measurement(session.id, stream, number) for number in (0, 1, 2)),
        measurement(session.id, stream, 3, settings={}),
        measurement(session.id, stream, 4, id=" exact/+ sweep "),
        measurement(session.id, stream, 5, id="😀" * 128),
    ])
    # Preserve the original session-created event between the two source groups.
    other = create_session(service)
    service.ingest_sweeps([
        measurement(other.id, other.streams[0].id, 50),
        *(measurement(session.id, session.streams[1].id, number,
                      frequencies_hz=list(range(16384)), levels=[-50] * 16384)
          for number in range(10, 19)),
    ])
    return session


class ContextHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(path))
        self.session = seed_context_http_archive(service)
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/sweep-context"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, payload=None, *, raw=None, path=None, method="POST"):
        if raw is None and method != "GET":
            raw = json.dumps(request_body() if payload is None else payload).encode()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path or self.root, body=raw, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw_headers(self, headers, body=b"", *, truncated=False):
        with socket.create_connection(self.server.server_address, timeout=5) as connection:
            connection.sendall((f"POST {self.root} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n" + headers + "\r\n").encode() + body)
            if truncated:
                connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            result = response.status, json.loads(response.read())
            self.assertEqual(connection.recv(1), b"")
            return result

    def test_success_capacity_envelope_missing_counts_exact_ids_and_capability(self):
        service = self.server.session_service
        before = service.health(), service.events(), service.archive_capacity(), list(service._store._connection.iterdump())
        status, envelope = self.request(request_body("sweep-1", 3, 1))
        self.assertEqual(status, 200)
        self.assertEqual((envelope["contract"], envelope["contract_version"]), ("openradioinspector-platform", 1))
        self.assertEqual(envelope["data"]["missing"], {"before": 2, "after": 0})
        self.assertEqual([s["offset"] for s in envelope["data"]["items"]], [-1, 0, 1])
        self.assertEqual((service.health(), service.events(), service.archive_capacity(), list(service._store._connection.iterdump())), before)
        self.assertEqual(before[2]["state"], "full")
        for sweep_id in (" exact/+ sweep ", "😀" * 128):
            status, envelope = self.request(request_body(sweep_id))
            self.assertEqual((status, envelope["data"]["anchor_id"]), (200, sweep_id))
        status, envelope = self.request(path="/api/v1/capabilities", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(envelope["data"]["features"]["sweep_context"])

    def test_strict_json_utf8_duplicate_fields_shapes_types_bounds(self):
        valid = request_body()
        invalid = [b"", b"{", b"[]", b"null", b"true", b"{}", b"\xff", b"\xef\xbb\xbf{}",
                   json.dumps(dict(valid, extra=1)).encode(), json.dumps({"sweep_id": "sweep-0"}).encode(),
                   b"[" * 1200 + b"0" + b"]" * 1200]
        for extra in ('"sweep_id":"sweep-0"', '"before":0', '"after":0', '"extra":{"x":1,"x":2}',
                      '"extra":NaN', '"extra":Infinity', '"extra":1e999', '"extra":"\\ud800"'):
            invalid.append((json.dumps(valid)[:-1] + ',' + extra + '}').encode())
        for value in (None, True, 10, "", "  ", "\ud800", "x" * 129, "😀" * 129):
            invalid.append(json.dumps(dict(valid, sweep_id=value)).encode())
        for field in ("before", "after"):
            for value in (None, True, False, "1", -1, 17, 1.0, {}, [], float("nan"), float("inf"), 10 ** 1000):
                invalid.append(json.dumps(dict(valid, **{field: value})).encode())
        before = self.server.session_service.health()
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                status, envelope = self.request(raw=raw)
                self.assertEqual((status, envelope["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.server.session_service.health(), before)

    def test_missing_foreign_incompatible_neighbour_and_total_overflow_errors(self):
        before = self.server.session_service.health(), list(self.server.session_service._store._connection.iterdump())
        for payload, status, code in ((request_body("missing"), 404, "sweep_not_found"),
                                      (request_body("sweep-50"), 404, "sweep_not_found"),
                                      (request_body("sweep-3"), 409, "context_incompatible"),
                                      (request_body("sweep-2", 0, 1), 409, "context_incompatible"),
                                      (request_body("sweep-14", 4, 4), 409, "context_incompatible")):
            result, envelope = self.request(payload)
            self.assertEqual((result, envelope["error"]["code"]), (status, code))
            self.assertNotIn("data", envelope)
        status, envelope = self.request(path="/api/v1/sessions/missing/sweep-context")
        self.assertEqual((status, envelope["error"]["code"]), (404, "session_not_found"))
        self.assertEqual((self.server.session_service.health(), list(self.server.session_service._store._connection.iterdump())), before)

    def test_no_query_even_bare_question_mark_and_get_unsupported(self):
        for suffix in ("?", "?x=1", "?x=", "?&", "?sweep_id=sweep-0"):
            status, envelope = self.request(path=self.root + suffix)
            self.assertEqual((status, envelope["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.request(method="GET")[0], 404)

    def test_body_size_exact_limit_oversize_before_read_and_leading_zero_length(self):
        for size in (str(MAX_CONTEXT_REQUEST_BYTES + 1), "9" * 5000):
            status, envelope = self.raw_headers(f"Content-Length: {size}\r\n")
            self.assertEqual((status, envelope["error"]["code"]), (413, "request_too_large"))
        raw = json.dumps(request_body()).encode()
        self.assertEqual(self.request(raw=raw + b" " * (MAX_CONTEXT_REQUEST_BYTES - len(raw)))[0], 200)
        self.assertEqual(self.raw_headers(f"Content-Length: 000{len(raw)}\r\n", raw)[0], 200)

    def test_duplicate_invalid_framing_transfer_encoding_and_truncated_body(self):
        invalid = ["", "Content-Length: -1\r\n", "Content-Length: +1\r\n", "Content-Length: 1.0\r\n",
                   "Content-Length: 0, 0\r\n", "Content-Length: 0\r\nContent-Length: 0\r\n",
                   "Content-Length: 0\r\nTransfer-Encoding: chunked\r\n",
                   "Content-Length: 0\r\nTransfer-Encoding: identity\r\n",
                   "Content-Length: \r\n", "Transfer-Encoding: chunked\r\n"]
        for headers in invalid:
            with self.subTest(headers=headers):
                status, envelope = self.raw_headers(headers)
                self.assertEqual((status, envelope["error"]["code"]), (400, "invalid_request"))
        status, envelope = self.raw_headers("Content-Length: 100\r\n", b"{", truncated=True)
        self.assertEqual((status, envelope["error"]["code"]), (400, "invalid_request"))


if __name__ == "__main__":
    unittest.main()
