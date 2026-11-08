"""Read-only archived sweep comparison: model, storage, restart and HTTP."""

from __future__ import annotations

import copy
import http.client
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, urlencode

from radioinspector.contracts import Sweep
from radioinspector.session_service import SessionNotFoundError, SessionService, SweepNotFoundError
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.sweep_comparison import (
    MAX_COMPARISON_POINTS,
    ComparisonIncompatibleError,
    compare_sweeps,
)
from radioinspector.web.server import create_server
from tests.test_persistence import sweep_payload
from tests.test_platform import session_payload


def measurement(session_id="session", stream_id="stream", number=0, **changes):
    payload = sweep_payload(session_id, stream_id, number)
    payload.update({
        "frequencies_hz": [100, 200, 300, 400],
        "levels": [-80, -70, -60, -50],
        "settings": {"rbw_hz": 10_000, "sequence": number},
    })
    payload.update(changes)
    return payload


class SweepComparisonTests(unittest.TestCase):
    def compare(self, **changes):
        reference = Sweep.from_dict(measurement())
        target = Sweep.from_dict(measurement(number=1, **changes))
        return compare_sweeps("session", reference, target)

    def test_signed_deltas_originals_and_deterministic_ties(self):
        result = self.compare(levels=[-75, -75, -55, -55])
        self.assertEqual(result["delta_levels"], [5, -5, 5, -5])
        self.assertEqual(result["frequencies_hz"], [100, 200, 300, 400])
        self.assertEqual(result["points"], 4)
        self.assertEqual(result["delta_unit"], "dB")
        self.assertEqual(result["max_increase"], {"frequency_hz": 100, "delta_db": 5})
        self.assertEqual(result["min_change"], {"frequency_hz": 200, "delta_db": -5})
        self.assertEqual(result["reference"]["levels"], [-80, -70, -60, -50])
        self.assertEqual(result["target"]["levels"], [-75, -75, -55, -55])
        self.assertEqual(result["reference"]["settings"]["sequence"], 0)
        self.assertEqual(result["target"]["settings"]["sequence"], 1)
        self.assertEqual(result["reference"]["interval"], measurement()["interval"])
        self.assertEqual(result["target"]["interval"], measurement(number=1)["interval"])
        for field in ("reference", "target"):
            self.assertEqual(result[field]["level_unit"], "dBm")
        self.assertEqual(result["stream_id"], "stream")
        self.assertEqual(result["session_id"], "session")

    def test_zero_all_negative_and_all_positive_extrema(self):
        for levels, maximum, minimum in (
            ([-80, -70, -60, -50], 0, 0),
            ([-81, -72, -63, -54], -1, -4),
            ([-79, -68, -57, -46], 4, 1),
        ):
            with self.subTest(levels=levels):
                result = self.compare(levels=levels)
                self.assertEqual(result["max_increase"]["delta_db"], maximum)
                self.assertEqual(result["min_change"]["delta_db"], minimum)

    def test_duplicate_bins_preserved_and_first_bin_wins(self):
        reference = Sweep.from_dict(measurement(frequencies_hz=[100, 100, 200, 200]))
        target = copy.deepcopy(reference)
        target.levels = (-75, -65, -65, -55)
        result = compare_sweeps("session", reference, target)
        self.assertEqual(result["frequencies_hz"], [100, 100, 200, 200])
        self.assertEqual(result["delta_levels"], [5, 5, -5, -5])
        self.assertEqual(result["max_increase"]["frequency_hz"], 100)
        self.assertEqual(result["min_change"]["frequency_hz"], 200)

    def test_incompatible_session_stream_grid_units_and_settings(self):
        for changes in (
            {"session_id": "other"}, {"stream_id": "other"},
            {"frequencies_hz": [100, 200, 300, 401]},
            {"frequencies_hz": [100, 200], "levels": [-80, -70]},
            {"level_unit": "s_meter_count"}, {"level_unit": "dB"},
            {"settings": {"rbw_hz": 20_000}},
            {"settings": {"rbw_hz": 10_000, "unknown_extension": True}},
            {"settings": {}}, {"settings": {"sequence": 10}},
            {"settings": {"rbw_hz": None}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ComparisonIncompatibleError):
                self.compare(**changes)

    def test_settings_type_aware_structural_equality(self):
        settings = {"rbw_hz": 10_000, "extension": {"a": [1, True, "on"], "b": False}}
        reference = Sweep.from_dict(measurement(settings=settings))
        equivalent = {"extension": {"b": False, "a": [1.0, True, "on"]}, "rbw_hz": 10_000.0}
        target = Sweep.from_dict(measurement(settings=equivalent))
        self.assertEqual(compare_sweeps("session", reference, target)["delta_levels"], [0] * 4)
        for value in ([True, True, "on"], [1, 1, "on"], [1, True, False], [1, "on", True]):
            with self.subTest(value=value):
                target.settings["extension"]["a"] = value
                with self.assertRaises(ComparisonIncompatibleError):
                    compare_sweeps("session", reference, target)

    def test_only_top_level_sequence_is_ignored(self):
        reference = Sweep.from_dict(measurement(settings={"extension": {"sequence": 1}}))
        target = Sweep.from_dict(measurement(settings={"extension": {"sequence": 2}}))
        with self.assertRaises(ComparisonIncompatibleError):
            compare_sweeps("session", reference, target)
        self.compare(settings={"rbw_hz": 10_000, "sequence": None})

    def test_invalid_settings_rejected_even_when_identical(self):
        for settings in ({}, {"sequence": 1}, {"rbw_hz": None},
                         {"extension": [None]}, {"rbw_hz": float("nan")},
                         {"rbw_hz": float("inf")}, {"extension": object()},
                         {"rbw_hz": 1, "sequence": float("nan")}, {1: "x"}):
            with self.subTest(settings=settings):
                sweep = Sweep.from_dict(measurement(settings=settings))
                with self.assertRaises(ComparisonIncompatibleError):
                    compare_sweeps("session", sweep, sweep)

    def test_finite_inputs_cannot_overflow_delta(self):
        reference = Sweep.from_dict(measurement(levels=[-1e308] * 4))
        target = Sweep.from_dict(measurement(levels=[1e308] * 4))
        with self.assertRaisesRegex(ComparisonIncompatibleError, "числовой диапазон"):
            compare_sweeps("session", reference, target)

    def test_limit_boundary_preserves_every_bin_or_rejects(self):
        for count in (MAX_COMPARISON_POINTS, MAX_COMPARISON_POINTS + 1):
            sweep = Sweep.from_dict(measurement(
                frequencies_hz=list(range(count)), levels=[-80] * count,
            ))
            if count == MAX_COMPARISON_POINTS:
                result = compare_sweeps("session", sweep, sweep)
                self.assertEqual(result["points"], count)
                self.assertEqual(len(result["delta_levels"]), count)
                self.assertEqual(result["frequencies_hz"][-1], count - 1)
            else:
                with self.assertRaises(ComparisonIncompatibleError):
                    compare_sweeps("session", sweep, sweep)


class ComparisonServiceMixin:
    def add(self, number=0, **changes):
        return self.service.ingest_sweep(measurement(self.session.id, self.stream_id, number, **changes))

    def compare(self, reference_id="sweep-0", target_id="sweep-1"):
        return self.service.compare_sweeps(self.session.id, reference_id=reference_id, target_id=target_id)

    def test_reference_ids_beyond_recent_window(self):
        for start, end in ((0, 100), (100, 112)):
            self.service.ingest_sweeps([
                measurement(self.session.id, self.stream_id, i) for i in range(start, end)
            ])
        self.assertNotIn("sweep-0", [s.id for s in self.service.list_sweeps(self.session.id)])
        result = self.compare()
        self.assertEqual(result["reference"]["id"], "sweep-0")
        self.assertEqual(result["target"]["id"], "sweep-1")
        self.assertEqual(result["delta_levels"], [0] * 4)

    def test_same_id_and_independent_response(self):
        original = self.add(settings={"rbw_hz": 10_000, "extension": {"x": [1]}})
        result = self.compare(original.id, original.id)
        self.assertEqual(result["delta_levels"], [0] * 4)
        result["reference"]["settings"]["extension"]["x"].append(2)
        result["reference"]["levels"][0] = 100
        result["frequencies_hz"][0] = 999
        self.assertEqual(result["target"]["settings"]["extension"]["x"], [1])
        self.assertEqual(self.service.list_sweeps(self.session.id), [original])

    def test_no_mutation_events_or_cursor_changes_on_success_and_refusal(self):
        self.add()
        self.add(1, levels=[-70, -60, -50, -40])
        self.add(2, settings={"rbw_hz": 99})
        before = (self.service.health(), self.service.events(), self.service.list_sessions(),
                  self.service.list_sweeps(self.session.id), self.service.archive_capacity())
        subscriber_id, subscriber = self.service.subscribe(after=before[0]["last_event_id"])
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        self.compare()
        with self.assertRaises(ComparisonIncompatibleError):
            self.compare(target_id="sweep-2")
        after = (self.service.health(), self.service.events(), self.service.list_sessions(),
                 self.service.list_sweeps(self.session.id), self.service.archive_capacity())
        self.assertEqual(after, before)
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        self.add(3)
        self.assertEqual(self.service.health()["last_event_id"], before[0]["last_event_id"] + 1)

    def test_missing_session_sweep_and_invalid_ids(self):
        self.add()
        with self.assertRaises(SessionNotFoundError):
            self.service.compare_sweeps("missing", reference_id="sweep-0", target_id="sweep-0")
        for reference, target in (("missing", "sweep-0"), ("sweep-0", "missing")):
            with self.subTest(reference=reference), self.assertRaises(SweepNotFoundError):
                self.compare(reference, target)
        for value in (None, "", "  ", 10, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.compare(value, "sweep-0")

    def test_cross_session_and_cross_stream_rejected(self):
        self.add()
        second = self.service.create_session(session_payload())
        self.service.ingest_sweep(measurement(second.id, second.streams[0].id, 2))
        with self.assertRaises(SweepNotFoundError):
            self.compare(target_id="sweep-2")
        self.service.ingest_sweep(measurement(self.session.id, self.session.streams[1].id, 3))
        with self.assertRaises(ComparisonIncompatibleError):
            self.compare(target_id="sweep-3")

    def test_settings_and_overflow_rejections_consistent(self):
        for number, settings in enumerate(({}, {"sequence": 1}, {"rbw_hz": None}, {"x": [None]})):
            item = self.add(number, settings=settings)
            with self.subTest(settings=settings), self.assertRaises(ComparisonIncompatibleError):
                self.compare(item.id, item.id)
        self.add(10, levels=[-1e308] * 4)
        self.add(11, levels=[1e308] * 4)
        with self.assertRaises(ComparisonIncompatibleError):
            self.compare("sweep-10", "sweep-11")

    def test_capability(self):
        self.assertTrue(self.service.capabilities()["features"]["sweep_comparison"])


def create_session(service):
    payload = session_payload()
    payload["streams"][1].update(role="spectrum_analyzer", level_unit="dBm")
    return service.create_session(payload)


class MemoryComparisonTests(ComparisonServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteComparisonTests(ComparisonServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(lambda: self.service.close())
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id

    def test_primary_key_reads_without_archive_scan_or_database_writes(self):
        self.add()
        self.add(1)
        statements = []
        before = self.store._connection.total_changes
        self.store._connection.set_trace_callback(statements.append)
        try:
            with patch.object(self.store, "list_sweeps", side_effect=AssertionError("no archive scan")):
                self.compare()
        finally:
            self.store._connection.set_trace_callback(None)
        self.assertEqual(self.store._connection.total_changes, before)
        self.assertEqual(len(statements), 2)
        self.assertTrue(all(statement.startswith("SELECT * FROM sweeps WHERE id = ") for statement in statements))
        plan = self.store._connection.execute("EXPLAIN QUERY PLAN SELECT * FROM sweeps WHERE id = ?", ("sweep-0",)).fetchall()
        self.assertTrue(any("SEARCH sweeps USING INDEX" in row[3] for row in plan))

    def test_restart_full_archive_keeps_comparison_readable(self):
        self.add()
        self.add(1, levels=[-75, -75, -55, -55])
        expected = self.compare()
        size = self.service.archive_capacity()["database_bytes"]
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=size)
        self.service = SessionService(store=self.store, history_limit=2)
        self.assertEqual(self.service.archive_capacity()["state"], "full")
        self.assertEqual(self.compare(), expected)


class ComparisonHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(path))
        self.session = create_session(service)
        stream = self.session.streams[0].id
        service.ingest_sweep(measurement(self.session.id, stream))
        service.ingest_sweep(measurement(self.session.id, stream, 1, levels=[-75, -75, -55, -55]))
        service.ingest_sweep(measurement(self.session.id, stream, 2, settings={"rbw_hz": 20_000}))
        service.ingest_sweep(measurement(self.session.id, stream, 3, id=" encoded/+ sweep "))
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/sweep-comparison"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, query, root=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", (root or self.root) + query)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_success_at_capacity_preserves_envelope_events_and_source(self):
        service = self.server.session_service
        before = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity())
        status, envelope = self.request("?reference_id=sweep-0&target_id=sweep-1")
        self.assertEqual(status, 200)
        self.assertEqual(envelope["contract_version"], 1)
        self.assertEqual(envelope["data"]["delta_levels"], [5, -5, 5, -5])
        after = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity())
        self.assertEqual(before, after)
        self.assertEqual(after[-1]["state"], "full")
        status, capabilities = self.request("", root="/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(capabilities["data"]["features"]["sweep_comparison"])

    def test_invalid_query_parameters(self):
        for query in ("", "?", "?reference_id=sweep-0", "?target_id=sweep-1",
                      "?reference_id=&target_id=sweep-1", "?reference_id=%20%20&target_id=sweep-1",
                      "?reference_id=sweep-0&target_id=", "?reference_id=sweep-0&target_id",
                      "?reference_id=sweep-0&reference_id=sweep-0&target_id=sweep-1",
                      "?reference_id=sweep-0&reference_id=&target_id=sweep-1",
                      "?reference_id=sweep-0&target_id=sweep-1&target_id=sweep-1",
                      "?reference_id=sweep-0&target_id=sweep-1&stream_id=x",
                      "?reference_id=sweep-0&target_id=sweep-1&", "?reference_id=%ZZ&target_id=sweep-1",
                      "?reference_id=%&target_id=sweep-1", "?reference_id=%FF&target_id=sweep-1"):
            with self.subTest(query=query):
                status, response = self.request(query)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"]["code"], "invalid_request")

    def test_error_classification(self):
        for query, status_expected, code in (
            ("?reference_id=missing&target_id=sweep-1", 404, "sweep_not_found"),
            ("?reference_id=sweep-0&target_id=missing", 404, "sweep_not_found"),
            ("?reference_id=sweep-0&target_id=sweep-2", 409, "comparison_incompatible"),
        ):
            with self.subTest(query=query):
                status, response = self.request(query)
                self.assertEqual(status, status_expected)
                self.assertEqual(response["error"]["code"], code)
        status, response = self.request("?reference_id=sweep-0&target_id=sweep-1", root="/api/v1/sessions/missing/sweep-comparison")
        self.assertEqual(status, 404)
        self.assertEqual(response["error"]["code"], "session_not_found")

    def test_ids_keep_whitespace_slashes_and_plus(self):
        original_id = " encoded/+ sweep "
        query = "?" + urlencode({"reference_id": original_id, "target_id": original_id}, quote_via=quote)
        status, result = self.request(query)
        self.assertEqual(status, 200)
        self.assertEqual(result["data"]["reference"]["id"], original_id)
        self.assertEqual(result["data"]["delta_levels"], [0] * 4)
        status, response = self.request("?" + urlencode({"reference_id": original_id.strip(), "target_id": original_id}))
        self.assertEqual(status, 404)
        self.assertEqual(response["error"]["code"], "sweep_not_found")


if __name__ == "__main__":
    unittest.main()
