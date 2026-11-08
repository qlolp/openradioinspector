"""Synthetic-only independent horizontal thresholds: math, storage and HTTP."""

from __future__ import annotations

import copy
import http.client
import json
import math
import queue
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from radioinspector.contracts import Sweep, to_primitive
from radioinspector.session_service import SessionNotFoundError, SessionService, SweepNotFoundError
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.sweep_comparison import MAX_COMPARISON_POINTS, validate_sweep_compatibility
from radioinspector.sweep_three_thresholds import (
    MAX_FREQUENCY_HZ, MAX_THREE_THRESHOLDS_REQUEST_BYTES, ThreeThresholdsIncompatibleError,
    calculate_sweep_three_thresholds, validate_three_thresholds_request,
)
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session, measurement


def request_body(sweep_id="sweep-0", levels_dbm=None):
    return {"sweep_id": sweep_id, "levels_dbm": [-75, -65, -55] if levels_dbm is None else levels_dbm}


class ThreeThresholdsArithmeticTests(unittest.TestCase):
    def calculate(self, levels=(-80, -70, -60, -50), thresholds=(-75, -65, -55), **changes):
        payload = measurement(frequencies_hz=list(range(len(levels))), levels=levels)
        payload.update(changes)
        return calculate_sweep_three_thresholds("session", Sweep.from_dict(payload), levels_dbm=list(thresholds))

    def test_independent_signed_excess_order_and_overlapping_counts(self):
        result = self.calculate(thresholds=(-55, -75, -65))
        self.assertEqual(set(result), {"session_id", "stream_id", "sweep", "frequencies_hz", "method",
                                       "comparison", "level_unit", "delta_unit", "points", "lines"})
        self.assertEqual((result["method"], result["comparison"], result["level_unit"], result["delta_unit"]),
                         ("three_horizontal", "strictly_greater", "dBm", "dB"))
        self.assertEqual((result["session_id"], result["stream_id"], result["points"]), ("session", "stream", 4))
        for line, number, threshold, excess, count in zip(result["lines"], (1, 2, 3), (-55, -75, -65),
                ([-25, -15, -5, 5], [-5, 5, 15, 25], [-15, -5, 5, 15]), (1, 3, 2)):
            self.assertEqual(set(line), {"id", "level_dbm", "excess_levels", "regions", "exceedance_points", "region_count"})
            self.assertEqual(line["id"], f"line-{number}")
            self.assertEqual(line["level_dbm"], threshold)
            self.assertEqual(line["excess_levels"], excess)
            self.assertEqual((line["exceedance_points"], line["region_count"]), (count, 1))
        self.assertGreater(sum(line["exceedance_points"] for line in result["lines"]), result["points"])

    def test_equal_lines_stay_independent_strict_equality_and_signed_values(self):
        result = self.calculate(levels=(-80, -70, -60), thresholds=(-70,) * 3)
        for index, line in enumerate(result["lines"], start=1):
            self.assertEqual(line["id"], f"line-{index}")
            self.assertEqual(line["excess_levels"], [-10, 0, 10])
            self.assertEqual(line["exceedance_points"], 1)
            self.assertEqual(line["regions"][0]["start_index"], 2)
        result["lines"][0]["excess_levels"][2] = 999
        result["lines"][0]["regions"][0]["points"] = 999
        self.assertEqual(result["lines"][1]["excess_levels"], [-10, 0, 10])
        self.assertEqual(result["lines"][2]["regions"][0]["points"], 1)
        for levels in ((-70,) * 3, (-80,) * 3):
            self.assertTrue(all(line["regions"] == [] for line in self.calculate(levels, (-70,) * 3)["lines"]))
        for line in self.calculate((-60,) * 3, (-70,) * 3)["lines"]:
            self.assertEqual((line["region_count"], line["exceedance_points"]), (1, 3))

    def test_single_bin_zero_and_safe_integer_boundaries(self):
        for frequency in (0, MAX_FREQUENCY_HZ):
            result = self.calculate((-70,), (-70, -80, -60), frequencies_hz=[frequency])
            self.assertEqual(result["points"], 1)
            self.assertEqual(result["frequencies_hz"], [frequency])
            self.assertEqual([line["exceedance_points"] for line in result["lines"]], [0, 1, 0])
            self.assertEqual(result["lines"][1]["regions"], [{
                "start_index": 0, "end_index": 0, "start_frequency_hz": frequency, "end_frequency_hz": frequency,
                "peak_index": 0, "peak_frequency_hz": frequency, "peak_excess_db": 10, "points": 1,
            }])

    def test_inclusive_index_regions_duplicate_nonuniform_bins_and_first_peak_tie(self):
        frequencies = [0, 0, 0, 1_000_000, 1_000_000, 1_000_001, MAX_FREQUENCY_HZ]
        result = self.calculate((-60, -70, -60, -50, -50, -70, -60), (-70,) * 3, frequencies_hz=frequencies)
        self.assertEqual(result["frequencies_hz"], frequencies)
        for line in result["lines"]:
            self.assertEqual([(r["start_index"], r["end_index"], r["peak_index"], r["points"])
                              for r in line["regions"]], [(0, 0, 0, 1), (2, 4, 3, 3), (6, 6, 6, 1)])
            self.assertEqual(line["regions"][1]["start_frequency_hz"], 0)
            self.assertEqual(line["regions"][1]["end_frequency_hz"], 1_000_000)
            self.assertEqual(line["regions"][1]["peak_excess_db"], 20)
            self.assertEqual((line["region_count"], line["exceedance_points"]), (3, 5))

    def test_source_and_full_provenance_are_detached(self):
        sweep = Sweep.from_dict(measurement(settings={"rbw_hz": 100, "sequence": None, "extension": {"values": [1]}}))
        original = copy.deepcopy(sweep)
        result = calculate_sweep_three_thresholds("session", sweep, levels_dbm=[-75, -65, -55])
        self.assertEqual(result["sweep"], {"id": sweep.id, "interval": to_primitive(sweep.interval),
                          "settings": sweep.settings, "levels": list(sweep.levels), "level_unit": "dBm"})
        result["sweep"]["settings"]["extension"]["values"].append(2)
        result["sweep"]["settings"]["sequence"] = 10
        result["sweep"]["levels"][0] = 1
        result["sweep"]["interval"]["started_at"] = "changed"
        result["frequencies_hz"][0] = 999
        self.assertEqual(sweep, original)

    def test_finite_extremes_and_overflow_on_any_line_reject_everything(self):
        maximum, tiny = sys.float_info.max, math.ulp(0.0)
        for value in (-maximum, maximum, -tiny, tiny, -0.0, 0.0):
            result = self.calculate((value,), (value,) * 3)
            self.assertEqual([line["excess_levels"] for line in result["lines"]], [[0]] * 3)
            json.dumps(result, allow_nan=False)
        for source, boundary in ((maximum, -maximum), (-maximum, maximum)):
            for index in range(3):
                thresholds = [source] * 3
                thresholds[index] = boundary
                with self.subTest(source=source, index=index), self.assertRaises(ThreeThresholdsIncompatibleError):
                    self.calculate((source,), thresholds)
        result = self.calculate((tiny, -tiny), (0,) * 3)
        self.assertEqual(result["lines"][0]["excess_levels"], [tiny, -tiny])

    def test_maximum_bins_maximum_regions_and_no_truncation(self):
        count = MAX_COMPARISON_POINTS
        result = self.calculate((-80, -60) * (count // 2), (-70,) * 3)
        self.assertEqual(result["points"], count)
        self.assertEqual(result["frequencies_hz"][-1], count - 1)
        for line in result["lines"]:
            self.assertEqual(len(line["excess_levels"]), count)
            self.assertEqual((line["region_count"], line["exceedance_points"]), (count // 2, count // 2))
            self.assertEqual(line["regions"][-1]["end_index"], count - 1)
        with self.assertRaises(ThreeThresholdsIncompatibleError):
            self.calculate((-80,) * (count + 1))

    def test_shared_compatibility_gate_and_source_rejections(self):
        sweep = Sweep.from_dict(measurement())
        with patch("radioinspector.sweep_three_thresholds.validate_sweep_compatibility",
                   wraps=validate_sweep_compatibility) as gate:
            calculate_sweep_three_thresholds("session", sweep, levels_dbm=[-70] * 3)
            gate.assert_called_once_with("session", sweep, sweep)
        for changes in ({"session_id": "other"}, {"level_unit": "s_meter_count"}, {"level_unit": "dB"},
                        {"settings": {}}, {"settings": {"sequence": 1}}, {"settings": {"rbw_hz": None}},
                        {"settings": {"extension": [None]}}, {"settings": {"x": float("inf")}},
                        {"settings": {"x": object()}}, {"settings": {"rbw_hz": 100, "sequence": float("nan")}}):
            with self.subTest(changes=changes), self.assertRaises(ThreeThresholdsIncompatibleError):
                self.calculate(**changes)
        for frequencies in ((MAX_FREQUENCY_HZ + 1,), (-1,), (True,), (1.0,)):
            sweep = Sweep.from_dict(measurement(frequencies_hz=[0], levels=[-70]))
            sweep.frequencies_hz = frequencies
            with self.subTest(frequencies=frequencies), self.assertRaises(ThreeThresholdsIncompatibleError):
                calculate_sweep_three_thresholds("session", sweep, levels_dbm=[-70] * 3)
        for levels in ((), (1,), (float("inf"),) * 4, (float("nan"),) * 4):
            sweep = Sweep.from_dict(measurement())
            sweep.levels = levels
            with self.assertRaises(ThreeThresholdsIncompatibleError):
                calculate_sweep_three_thresholds("session", sweep, levels_dbm=[-70] * 3)
        sweep.levels = sweep.frequencies_hz = ()
        with self.assertRaises(ThreeThresholdsIncompatibleError):
            calculate_sweep_three_thresholds("session", sweep, levels_dbm=[-70] * 3)

    def test_request_shape_types_utf8_codepoint_limits_and_no_normalization(self):
        invalid = [None, [], True, {}, dict(request_body(), extra=1), {"sweep_id": "x"}, {"levels_dbm": [1] * 3}]
        invalid += [request_body(value) for value in (None, True, 1, {}, [], "", " \t\n", "x" * 129, "😀" * 129, "\ud800")]
        invalid += [dict(request_body(), levels_dbm=value) for value in (None, True, {}, "123", (), [], [1], [1, 2], [1] * 4)]
        for value in (None, True, False, "-70", {}, [], float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            for index in range(3):
                levels = [-70] * 3
                levels[index] = value
                invalid.append(request_body(levels_dbm=levels))
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_three_thresholds_request(payload)
        for sweep_id in (" padded /+ id ", "😀" * 128, "x" * 128):
            self.assertEqual(validate_three_thresholds_request(request_body(sweep_id, [1, -2.5, 1])),
                             (sweep_id, [1.0, -2.5, 1.0]))


class ThreeThresholdsServiceMixin:
    def add(self, number=0, **changes):
        return self.service.ingest_sweep(measurement(self.session.id, self.stream_id, number, **changes))

    def calculate(self, sweep_id="sweep-0", levels_dbm=None):
        return self.service.sweep_three_thresholds(self.session.id, request_body(sweep_id, levels_dbm))

    def snapshot(self):
        return copy.deepcopy((self.service.health(), self.service.events(), self.service.list_sessions(),
                              self.service.list_sweeps(self.session.id), self.service.archive_capacity(),
                              self.service.list_sweep_references(self.session.id),
                              self.service.list_candidates(self.session.id), self.service.list_investigations(self.session.id)))

    def test_old_exact_id_under_one_lock_no_recent_list_lookup(self):
        for start, end in ((0, 100), (100, 112)):
            self.service.ingest_sweeps([measurement(self.session.id, self.stream_id, i) for i in range(start, end)])
        self.assertNotIn("sweep-0", [s.id for s in self.service.list_sweeps(self.session.id)])
        exact_reader = self.service._get_sweep_locked
        seen = []
        def checked_reader(session_id, sweep_id):
            self.assertTrue(self.service._lock._is_owned())
            seen.append(sweep_id)
            return exact_reader(session_id, sweep_id)
        with patch.object(self.service, "_get_sweep_locked", side_effect=checked_reader), \
                patch.object(self.service, "list_sweeps", side_effect=AssertionError("no recent-list scans")):
            result = self.calculate()
        self.assertEqual(seen, ["sweep-0"])
        self.assertEqual(result["sweep"]["id"], "sweep-0")

    def test_success_failure_response_edits_leave_state_and_event_cursor_unchanged(self):
        original = self.add(settings={"rbw_hz": 100, "extension": {"values": [1]}, "sequence": 0})
        self.add(1, settings={})
        self.add(2, levels=[sys.float_info.max] * 4)
        before = self.snapshot()
        subscriber_id, subscriber = self.service.subscribe(after=before[0]["last_event_id"])
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        result = self.calculate()
        result["sweep"]["settings"]["extension"]["values"].append(2)
        result["sweep"]["levels"][0] = 100
        result["frequencies_hz"][0] = 999
        result["lines"][0]["excess_levels"][0] = 999
        with self.assertRaises(ThreeThresholdsIncompatibleError):
            self.calculate("sweep-1")
        with self.assertRaises(ThreeThresholdsIncompatibleError):
            self.calculate("sweep-2", [0, 0, -sys.float_info.max])
        with self.assertRaises(ValueError):
            self.calculate(levels_dbm=[1, True, 3])
        with self.assertRaises(SweepNotFoundError):
            self.calculate("missing")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calculate()["sweep"]["levels"], list(original.levels))
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        self.add(3)
        self.assertEqual(self.service.health()["last_event_id"], before[0]["last_event_id"] + 1)

    def test_exact_missing_foreign_ids_and_capability(self):
        self.add(id=" encoded/+ sweep ")
        self.add(1, id="😀" * 128)
        for sweep_id in (" encoded/+ sweep ", "😀" * 128):
            self.assertEqual(self.calculate(sweep_id)["sweep"]["id"], sweep_id)
        second = create_session(self.service)
        self.service.ingest_sweep(measurement(second.id, second.streams[0].id, 3))
        for missing in ("missing", "sweep-3", "encoded/+ sweep"):
            with self.assertRaises(SweepNotFoundError):
                self.calculate(missing)
        with self.assertRaises(SessionNotFoundError):
            self.service.sweep_three_thresholds("missing", request_body())
        self.assertTrue(self.service.capabilities()["features"]["three_sweep_thresholds"])


class MemoryThreeThresholdsTests(ThreeThresholdsServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteMemoryThreeThresholdsTests(ThreeThresholdsServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(store=SQLiteSessionStore(":memory:"), history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteThreeThresholdsTests(ThreeThresholdsServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(lambda: self.service.close())
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id

    def test_only_primary_key_reads_no_writes_logical_dump_or_schema_changes(self):
        self.add()
        self.add(1, settings={})
        connection = self.store._connection
        before = (connection.total_changes, list(connection.iterdump()))
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            with patch.object(self.store, "list_sweeps", side_effect=AssertionError("no archive scan")):
                self.calculate()
                with self.assertRaises(ThreeThresholdsIncompatibleError):
                    self.calculate("sweep-1")
                with self.assertRaises(ValueError):
                    self.calculate(levels_dbm=[-70])
                with self.assertRaises(SweepNotFoundError):
                    self.calculate("missing")
        finally:
            connection.set_trace_callback(None)
        self.assertEqual((connection.total_changes, list(connection.iterdump())), before)
        self.assertEqual(len(statements), 3)
        self.assertTrue(all(statement.startswith("SELECT * FROM sweeps WHERE id = ") for statement in statements))
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM sweeps WHERE id = ?", ("sweep-0",)).fetchall()
        self.assertTrue(any("SEARCH sweeps USING INDEX" in row[3] for row in plan))

    def test_restart_at_capacity_full_keeps_old_exact_source_readable(self):
        self.test_old_exact_id_under_one_lock_no_recent_list_lookup()
        expected = self.calculate()
        size = self.service.archive_capacity()["database_bytes"]
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=size)
        self.service = SessionService(store=self.store, history_limit=2)
        self.assertEqual(self.service.archive_capacity()["state"], "full")
        before = list(self.store._connection.iterdump())
        self.assertEqual(self.calculate(), expected)
        self.assertEqual(list(self.store._connection.iterdump()), before)


class ThreeThresholdsHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(path))
        self.session = create_session(service)
        stream = self.session.streams[0].id
        service.ingest_sweep(measurement(self.session.id, stream))
        service.ingest_sweep(measurement(self.session.id, stream, 1, settings={}))
        service.ingest_sweep(measurement(self.session.id, stream, 2, id=" encoded/+ sweep "))
        second = create_session(service)
        service.ingest_sweep(measurement(second.id, second.streams[0].id, 3))
        service.ingest_sweep(measurement(self.session.id, stream, 4, levels=[sys.float_info.max] * 4))
        service.ingest_sweep(measurement(self.session.id, stream, 5, id="😀" * 128))
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/sweep-three-thresholds"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, payload=None, *, raw=None, path=None, method="POST"):
        if raw is None:
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
            connection.sendall((f"POST {self.root} HTTP/1.1\r\nHost: localhost\r\n" + headers + "\r\n").encode() + body)
            if truncated:
                connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            result = response.status, json.loads(response.read())
            self.assertEqual(connection.recv(1), b"")
            return result

    def test_success_at_capacity_envelope_exact_ids_and_capability(self):
        service = self.server.session_service
        before = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity(),
                  list(service._store._connection.iterdump()))
        status, envelope = self.request(request_body(levels_dbm=[-55, -75, -75]))
        self.assertEqual(status, 200)
        self.assertEqual((envelope["contract"], envelope["contract_version"]), ("openradioinspector-platform", 1))
        self.assertEqual([line["level_dbm"] for line in envelope["data"]["lines"]], [-55, -75, -75])
        self.assertEqual(envelope["data"]["lines"][1]["excess_levels"], [-5, 5, 15, 25])
        after = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity(),
                 list(service._store._connection.iterdump()))
        self.assertEqual(before, after)
        self.assertEqual(after[3]["state"], "full")
        status, capability = self.request(path="/api/v1/capabilities", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(capability["data"]["features"]["three_sweep_thresholds"])
        for sweep_id in (" encoded/+ sweep ", "😀" * 128):
            status, response = self.request(request_body(sweep_id))
            self.assertEqual(status, 200)
            self.assertEqual(response["data"]["sweep"]["id"], sweep_id)

    def test_strict_json_utf8_duplicate_keys_shapes_and_numbers(self):
        valid = request_body()
        invalid = [b"", b"{", b"[]", b"null", b"true", b"{}", b"\xff", b"\xef\xbb\xbf{}",
                   json.dumps(dict(valid, extra=1)).encode(), json.dumps({"sweep_id": "sweep-0"}).encode(),
                   json.dumps({"levels_dbm": [-70] * 3}).encode(), b"[" * 1200 + b"0" + b"]" * 1200]
        for extra in ('"sweep_id": "sweep-0"', '"levels_dbm": [-70, -60, -50]',
                      '"extra": {"x": 1, "x": 2}', '"extra": NaN', '"extra": Infinity',
                      '"extra": 1e999', '"extra": "\\ud800"'):
            invalid.append((json.dumps(valid)[:-1] + ', ' + extra + '}').encode())
        for value in (None, True, 10, "", "  ", "\ud800", "x" * 129, "😀" * 129):
            invalid.append(json.dumps(dict(valid, sweep_id=value)).encode())
        for value in (None, True, {}, "123", [], [1], [1, 2], [1] * 4):
            invalid.append(json.dumps(dict(valid, levels_dbm=value)).encode())
        for value in (None, True, False, "-70", {}, [], float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            for index in range(3):
                levels = [-70] * 3
                levels[index] = value
                invalid.append(json.dumps(dict(valid, levels_dbm=levels)).encode())
        invalid.append(b'{"sweep_id":"sweep-0","levels_dbm":[0,0,1e999]}')
        before = self.server.session_service.health()
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                status, response = self.request(raw=raw)
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.server.session_service.health(), before)

    def test_no_query_even_bare_question_mark_and_get_unsupported(self):
        for suffix in ("?", "?x=1", "?x=", "?&", "?sweep_id=sweep-0"):
            status, response = self.request(path=self.root + suffix)
            self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.request(method="GET")[0], 404)

    def test_missing_foreign_incompatible_and_any_line_overflow_mapping(self):
        before = (self.server.session_service.health(), self.server.session_service.events(),
                  list(self.server.session_service._store._connection.iterdump()))
        invalid = [(request_body("missing"), 404, "sweep_not_found"),
                   (request_body("sweep-3"), 404, "sweep_not_found"),
                   (request_body("sweep-1"), 409, "three_thresholds_incompatible")]
        for index in range(3):
            levels = [0] * 3
            levels[index] = -sys.float_info.max
            invalid.append((request_body("sweep-4", levels), 409, "three_thresholds_incompatible"))
        for payload, expected, code in invalid:
            status, response = self.request(payload)
            self.assertEqual((status, response["error"]["code"]), (expected, code))
            self.assertNotIn("data", response)
        status, response = self.request(path="/api/v1/sessions/missing/sweep-three-thresholds")
        self.assertEqual((status, response["error"]["code"]), (404, "session_not_found"))
        self.assertEqual((self.server.session_service.health(), self.server.session_service.events(),
                          list(self.server.session_service._store._connection.iterdump())), before)

    def test_size_bound_before_read_exact_boundary_and_leading_zero_length(self):
        for size in (str(MAX_THREE_THRESHOLDS_REQUEST_BYTES + 1), "9" * 5000):
            status, response = self.raw_headers(f"Content-Length: {size}\r\n")
            self.assertEqual((status, response["error"]["code"]), (413, "request_too_large"))
        raw = json.dumps(request_body()).encode()
        self.assertEqual(self.request(raw=raw + b" " * (MAX_THREE_THRESHOLDS_REQUEST_BYTES - len(raw)))[0], 200)
        self.assertEqual(self.raw_headers(f"Content-Length: 000{len(raw)}\r\n", raw)[0], 200)

    def test_framing_duplicate_malformed_length_transfer_encoding_and_truncation(self):
        invalid = ["", "Content-Length: -1\r\n", "Content-Length: +1\r\n", "Content-Length: 1.0\r\n",
                   "Content-Length: 0, 0\r\n", "Content-Length: 0\r\nContent-Length: 0\r\n",
                   "Content-Length: 0\r\nTransfer-Encoding: chunked\r\n",
                   "Content-Length: 0\r\nTransfer-Encoding: identity\r\n",
                   "Content-Length: \r\n", "Transfer-Encoding: chunked\r\n"]
        for headers in invalid:
            with self.subTest(headers=headers):
                status, response = self.raw_headers(headers)
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        status, response = self.raw_headers("Content-Length: 100\r\n", b"{", truncated=True)
        self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))


if __name__ == "__main__":
    unittest.main()
