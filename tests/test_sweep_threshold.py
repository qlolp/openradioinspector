"""Synthetic-only adaptive threshold checks: math, exact archive reads and HTTP."""

from __future__ import annotations

import copy
import http.client
import json
import math
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from radioinspector.contracts import Sweep
from radioinspector.session_service import SessionNotFoundError, SessionService, SweepNotFoundError
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.sweep_comparison import MAX_COMPARISON_POINTS
from radioinspector.sweep_threshold import ThresholdIncompatibleError, calculate_sweep_threshold
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session, measurement


def source(levels, **changes):
    payload = measurement(frequencies_hz=list(range(len(levels))), levels=levels)
    payload.update(changes)
    return Sweep.from_dict(payload)


class SweepThresholdTests(unittest.TestCase):
    def calculate(self, levels, window_points=3, margin_db=6, **changes):
        return calculate_sweep_threshold(
            "session", source(levels, **changes), window_points=window_points, margin_db=margin_db,
        )

    def test_isolated_spike_full_shape_and_provenance(self):
        sweep = source([-90, -90, -50, -90, -90], settings={"rbw_hz": 100, "x": [1]})
        before = copy.deepcopy(sweep)
        result = calculate_sweep_threshold("session", sweep, window_points=3, margin_db=6)
        self.assertEqual(set(result), {
            "session_id", "stream_id", "sweep", "frequencies_hz", "parameters",
            "baseline_levels", "threshold_levels", "level_unit", "excess_levels", "delta_unit",
            "regions", "points", "exceedance_points", "region_count",
        })
        self.assertEqual(result["session_id"], "session")
        self.assertEqual(result["stream_id"], "stream")
        self.assertEqual(result["sweep"], {
            "id": sweep.id, "interval": measurement()["interval"], "levels": list(sweep.levels),
            "level_unit": "dBm", "settings": sweep.settings,
        })
        self.assertEqual(result["parameters"], {
            "method": "rolling_median", "window_points": 3, "margin_db": 6,
            "edge_policy": "clip", "comparison": "strictly_greater",
        })
        self.assertEqual(result["baseline_levels"], [-90] * 5)
        self.assertEqual(result["threshold_levels"], [-84] * 5)
        self.assertEqual(result["excess_levels"], [-6, -6, 34, -6, -6])
        self.assertEqual(result["level_unit"], "dBm")
        self.assertEqual(result["delta_unit"], "dB")
        self.assertEqual(result["points"], 5)
        self.assertEqual(result["exceedance_points"], 1)
        self.assertEqual(result["region_count"], 1)
        self.assertEqual(result["regions"], [{
            "start_index": 2, "end_index": 2, "start_frequency_hz": 2, "end_frequency_hz": 2,
            "peak_index": 2, "peak_frequency_hz": 2, "peak_excess_db": 34, "points": 1,
        }])
        result["sweep"]["settings"]["x"].append(2)
        result["sweep"]["levels"][0] = 1
        result["sweep"]["interval"]["started_at"] = "changed"
        result["frequencies_hz"][0] = 999
        self.assertEqual(sweep, before)

    def test_slope_center_included_edges_clipped_even_medians(self):
        result = self.calculate([-100, -90, -80, -70, -60], margin_db=0)
        self.assertEqual(result["baseline_levels"], [-95, -90, -80, -70, -65])
        self.assertEqual(result["excess_levels"], [-5, 0, 0, 0, 5])
        result = self.calculate([-100, -90, -80, -70, -60], window_points=5, margin_db=0)
        self.assertEqual(result["baseline_levels"], [-90, -85, -80, -75, -70])
        self.assertEqual(self.calculate([-90, -50, -90], window_points=101)["baseline_levels"], [-90] * 3)

    def test_threshold_equality_and_signed_excess(self):
        result = self.calculate([-90, -84, -90], margin_db=6)
        self.assertEqual(result["excess_levels"], [-9, 0, -9])
        self.assertEqual(result["regions"], [])
        self.assertEqual(result["exceedance_points"], 0)
        self.assertEqual(result["region_count"], 0)
        self.assertEqual(self.calculate([-90] * 4, margin_db=0)["excess_levels"], [0] * 4)

    def test_index_regions_first_peak_tie_and_duplicate_nonuniform_hz(self):
        levels = [-50, -90, -50, -50, -90, -90, -40, -90, -90, -50, -90]
        frequencies = [1, 1, 1, 1_000_000, 1_000_000, 1_000_001, 9_000_000, 9_000_000, 9_000_001, 9_000_002, 9_000_003]
        result = self.calculate(levels, window_points=101, frequencies_hz=frequencies)
        self.assertEqual(result["frequencies_hz"], frequencies)
        self.assertEqual([(r["start_index"], r["end_index"], r["peak_index"], r["points"])
                          for r in result["regions"]], [(0, 0, 0, 1), (2, 3, 2, 2), (6, 6, 6, 1), (9, 9, 9, 1)])
        self.assertEqual(result["regions"][1]["start_frequency_hz"], 1)
        self.assertEqual(result["regions"][1]["end_frequency_hz"], 1_000_000)
        self.assertEqual(result["regions"][1]["peak_frequency_hz"], 1)
        self.assertEqual(result["regions"][2]["peak_excess_db"], 44)
        self.assertEqual(result["exceedance_points"], 5)
        self.assertEqual(result["region_count"], 4)
        trailing = self.calculate([-90, -90, -50], margin_db=0)
        self.assertEqual(trailing["regions"][-1]["end_index"], 2)

    def test_region_peak_uses_excess_maximum_not_first_positive_bin(self):
        result = self.calculate([-90, -90, -50, -40, -90, -90, -90], window_points=101)
        self.assertEqual(result["regions"], [{
            "start_index": 2, "end_index": 3, "start_frequency_hz": 2, "end_frequency_hz": 3,
            "peak_index": 3, "peak_frequency_hz": 3, "peak_excess_db": 44, "points": 2,
        }])

    def test_invalid_parameters(self):
        for window in (None, True, False, 0, 1, 2, 4, 100, 102, 103, 3.0, "3", float("nan")):
            with self.subTest(window=window), self.assertRaises(ValueError):
                self.calculate([-90] * 3, window_points=window)
        for margin in (None, True, False, -1, 121, "6", [], float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            with self.subTest(margin=margin), self.assertRaises(ValueError):
                self.calculate([-90] * 3, margin_db=margin)
        for margin in (0, 0.1, 120):
            self.assertEqual(self.calculate([-90] * 3, margin_db=margin)["parameters"]["margin_db"], margin)

    def test_extreme_finite_even_medians_without_overflow_or_underflow(self):
        largest = sys.float_info.max
        smallest = math.ulp(0.0)
        for value in (largest, -largest, smallest, -smallest):
            with self.subTest(value=value):
                result = self.calculate([value] * 3, margin_db=0)
                self.assertEqual(result["baseline_levels"], [value] * 3)
                self.assertEqual(result["excess_levels"], [0] * 3)
                json.dumps(result, allow_nan=False)
        self.assertEqual(self.calculate([-smallest, 0, 0], margin_db=0)["baseline_levels"][0], 0)
        self.assertEqual(self.calculate([0, smallest, smallest], margin_db=0)["baseline_levels"][0], 0)
        result = self.calculate([-largest, largest, 0], margin_db=0)
        self.assertEqual(result["baseline_levels"], [0, 0, largest / 2])
        with self.assertRaises(ThresholdIncompatibleError):
            self.calculate([-largest, -largest, largest], window_points=101, margin_db=0)

    def test_maximum_alternating_regions_preserve_every_bin(self):
        count = MAX_COMPARISON_POINTS
        result = self.calculate([-90, -50] * (count // 2), window_points=3)
        self.assertEqual(result["points"], count)
        self.assertEqual(result["exceedance_points"], count // 2)
        self.assertEqual(result["region_count"], count // 2)
        self.assertEqual(result["regions"][0]["start_index"], 1)
        self.assertEqual(result["regions"][-1]["end_index"], count - 1)
        self.assertTrue(all(region["points"] == 1 for region in result["regions"]))

    def test_source_gate_units_settings_session_and_point_limits(self):
        for changes in ({"session_id": "other"}, {"level_unit": "s_meter_count"}, {"level_unit": "dB"},
                        {"settings": {}}, {"settings": {"sequence": 1}}, {"settings": {"rbw_hz": None}},
                        {"settings": {"x": [None]}}, {"settings": {"x": float("inf")}},
                        {"settings": {"x": object()}}, {"settings": {"x": 1, "sequence": float("nan")}}):
            with self.subTest(changes=changes), self.assertRaises(ThresholdIncompatibleError):
                self.calculate([-90] * 3, **changes)
        for count in (1, 2, MAX_COMPARISON_POINTS + 1):
            with self.subTest(count=count), self.assertRaises(ThresholdIncompatibleError):
                self.calculate([-90] * count)
        result = self.calculate([-90] * MAX_COMPARISON_POINTS, window_points=101)
        self.assertEqual(result["points"], MAX_COMPARISON_POINTS)
        self.assertEqual(len(result["threshold_levels"]), MAX_COMPARISON_POINTS)
        self.assertEqual(result["frequencies_hz"][-1], MAX_COMPARISON_POINTS - 1)
        self.calculate([-90] * 3, settings={"rbw_hz": 100, "sequence": None})


class ThresholdServiceMixin:
    def add(self, number=0, **changes):
        return self.service.ingest_sweep(measurement(self.session.id, self.stream_id, number, **changes))

    def calculate(self, sweep_id="sweep-0", **changes):
        parameters = {"window_points": 3, "margin_db": 6}
        parameters.update(changes)
        return self.service.sweep_threshold(self.session.id, sweep_id=sweep_id, **parameters)

    def test_archived_id_outside_recent_window(self):
        for start, end in ((0, 100), (100, 112)):
            self.service.ingest_sweeps([measurement(self.session.id, self.stream_id, i) for i in range(start, end)])
        self.assertNotIn("sweep-0", [s.id for s in self.service.list_sweeps(self.session.id)])
        self.assertEqual(self.calculate()["sweep"]["id"], "sweep-0")

    def test_no_source_state_events_cursor_or_capacity_mutation(self):
        original = self.add(settings={"rbw_hz": 100, "extension": {"a": [1]}})
        self.add(1, settings={})
        self.add(2, frequencies_hz=[1, 2, 3], levels=[-1e308, -1e308, 1e308])
        before = copy.deepcopy((self.service.health(), self.service.events(), self.service.list_sessions(),
                                self.service.list_sweeps(self.session.id), self.service.archive_capacity()))
        subscriber_id, subscriber = self.service.subscribe(after=before[0]["last_event_id"])
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        result = self.calculate()
        result["sweep"]["settings"]["extension"]["a"].append(2)
        result["sweep"]["levels"][0] = 100
        result["frequencies_hz"][0] = 999
        for sweep_id in ("sweep-1", "sweep-2"):
            with self.assertRaises(ThresholdIncompatibleError):
                self.calculate(sweep_id, window_points=101)
        with self.assertRaises(ValueError):
            self.calculate(window_points=4)
        with self.assertRaises(SweepNotFoundError):
            self.calculate("missing")
        after = (self.service.health(), self.service.events(), self.service.list_sessions(),
                 self.service.list_sweeps(self.session.id), self.service.archive_capacity())
        self.assertEqual(after, before)
        self.assertEqual(self.calculate()["sweep"]["levels"], list(original.levels))
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        self.add(3)
        self.assertEqual(self.service.health()["last_event_id"], before[0]["last_event_id"] + 1)

    def test_missing_foreign_and_exact_ids(self):
        self.add(id=" encoded/+ sweep ")
        self.assertEqual(self.calculate(" encoded/+ sweep ")["sweep"]["id"], " encoded/+ sweep ")
        second = create_session(self.service)
        self.service.ingest_sweep(measurement(second.id, second.streams[0].id, 1))
        for sweep_id in ("missing", "sweep-1", "encoded/+ sweep"):
            with self.subTest(sweep_id=sweep_id), self.assertRaises(SweepNotFoundError):
                self.calculate(sweep_id)
        with self.assertRaises(SessionNotFoundError):
            self.service.sweep_threshold("missing", sweep_id="sweep-0", window_points=3, margin_db=6)
        for value in (None, "", "  ", 10, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.calculate(value)

    def test_capability(self):
        self.assertTrue(self.service.capabilities()["features"]["adaptive_sweep_threshold"])


class MemoryThresholdTests(ThresholdServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteThresholdTests(ThresholdServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(lambda: self.service.close())
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id

    def test_primary_key_read_no_scan_write_or_schema_change_success_and_failure(self):
        self.add()
        self.add(1, settings={})
        statements = []
        connection = self.store._connection
        before = connection.total_changes
        schema_before = list(connection.execute("SELECT * FROM sqlite_master"))
        connection.set_trace_callback(statements.append)
        try:
            with patch.object(self.store, "list_sweeps", side_effect=AssertionError("no archive scan")):
                self.calculate()
                with self.assertRaises(ThresholdIncompatibleError):
                    self.calculate("sweep-1")
        finally:
            connection.set_trace_callback(None)
        self.assertEqual(connection.total_changes, before)
        self.assertEqual(list(connection.execute("SELECT * FROM sqlite_master")), schema_before)
        self.assertEqual(len(statements), 2)
        self.assertTrue(all(s.startswith("SELECT * FROM sweeps WHERE id = ") for s in statements))

    def test_restart_at_capacity_preserves_analysis(self):
        self.add()
        for start, end in ((1, 101), (101, 112)):
            self.service.ingest_sweeps([measurement(self.session.id, self.stream_id, i) for i in range(start, end)])
        expected = self.calculate()
        size = self.service.archive_capacity()["database_bytes"]
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=size)
        self.service = SessionService(store=self.store, history_limit=2)
        self.assertEqual(self.service.archive_capacity()["state"], "full")
        self.assertNotIn("sweep-0", [s.id for s in self.service.list_sweeps(self.session.id)])
        self.assertEqual(self.calculate(), expected)


class ThresholdHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(path))
        self.session = create_session(service)
        stream = self.session.streams[0].id
        service.ingest_sweep(measurement(self.session.id, stream, frequencies_hz=[1, 2, 3, 4, 5],
                                       levels=[-90, -90, -50, -90, -90]))
        service.ingest_sweep(measurement(self.session.id, stream, 1, settings={}))
        service.ingest_sweep(measurement(self.session.id, stream, 2, id=" encoded/+ sweep "))
        second = create_session(service)
        service.ingest_sweep(measurement(second.id, second.streams[0].id, 3))
        service.ingest_sweep(measurement(self.session.id, stream, 4, frequencies_hz=[1, 2, 3],
                                       levels=[-1e308, -1e308, 1e308]))
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/sweep-threshold"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, query="?sweep_id=sweep-0&window_points=3&margin_db=6", root=None, method="GET"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, (root or self.root) + query,
                               body="{}" if method == "POST" else None,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_success_at_capacity_envelope_and_capability(self):
        service = self.server.session_service
        before = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity())
        status, envelope = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(envelope["contract_version"], 1)
        self.assertEqual(envelope["data"]["excess_levels"], [-6, -6, 34, -6, -6])
        after = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity())
        self.assertEqual(before, after)
        self.assertEqual(after[-1]["state"], "full")
        status, capabilities = self.request("", root="/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(capabilities["data"]["features"]["adaptive_sweep_threshold"])

    def test_strict_query_and_numeric_validation(self):
        valid = {"sweep_id": "sweep-0", "window_points": "3", "margin_db": "6"}
        invalid = ["", "?", "?" + urlencode(valid) + "&", "?" + urlencode(valid) + "&extra=x"]
        for name in valid:
            invalid += ["?" + urlencode({k: v for k, v in valid.items() if k != name}),
                        "?" + urlencode(valid) + "&" + name + "=" + valid[name],
                        "?" + urlencode(valid) + "&" + name + "="]
            for value in ("", "%20%20", "%ZZ", "%", "%FF"):
                invalid.append("?" + "&".join(k + "=" + (value if k == name else v) for k, v in valid.items()))
        for name, values in (("window_points", ("2", "4", "103", "3.0", "3e0", "true", "nan", "1_01")),
                             ("margin_db", ("-1", "121", "true", "nan", "inf", "-inf", "1e309", "1_0"))):
            for value in values:
                invalid.append("?" + urlencode(dict(valid, **{name: value})))
        before = self.server.session_service.health()
        for query in invalid:
            with self.subTest(query=query):
                status, response = self.request(query)
                self.assertEqual(status, 400)
                self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(self.server.session_service.health(), before)
        for window in ("3", "101"):
            for margin in ("0", "0.25", "6e0", "120"):
                self.assertEqual(self.request("?" + urlencode(dict(valid, window_points=window, margin_db=margin)))[0], 200)

    def test_not_found_incompatible_and_read_only_route(self):
        before = (self.server.session_service.health(), self.server.session_service.events(),
                  self.server.session_service.archive_capacity())
        for sweep_id, expected, code in (("missing", 404, "sweep_not_found"),
                                         ("sweep-3", 404, "sweep_not_found"),
                                         ("sweep-1", 409, "threshold_incompatible"),
                                         ("sweep-4", 409, "threshold_incompatible")):
            with self.subTest(sweep_id=sweep_id):
                status, response = self.request("?" + urlencode({"sweep_id": sweep_id, "window_points": 101, "margin_db": 6}))
                self.assertEqual(status, expected)
                self.assertEqual(response["error"]["code"], code)
        status, response = self.request(root="/api/v1/sessions/missing/sweep-threshold")
        self.assertEqual(status, 404)
        self.assertEqual(response["error"]["code"], "session_not_found")
        self.assertEqual(self.request("", method="POST")[0], 404)
        after = (self.server.session_service.health(), self.server.session_service.events(),
                 self.server.session_service.archive_capacity())
        self.assertEqual(before, after)

    def test_encoded_id_preserved(self):
        status, response = self.request("?" + urlencode({"sweep_id": " encoded/+ sweep ", "window_points": 3, "margin_db": 6}))
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["sweep"]["id"], " encoded/+ sweep ")


if __name__ == "__main__":
    unittest.main()
