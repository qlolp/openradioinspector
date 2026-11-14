"""Synthetic-only mean-power references: arithmetic, provenance, storage and HTTP."""

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
from radioinspector.sweep_average import (
    MAX_AVERAGE_REQUEST_BYTES, MAX_FREQUENCY_HZ, AverageReferenceIncompatibleError,
    calculate_sweep_average_comparison, validate_average_request,
)
from radioinspector.sweep_comparison import MAX_COMPARISON_POINTS
from radioinspector.web.server import create_server
from tests.test_persistence import measurement_interval
from tests.test_sweep_comparison import create_session, measurement


def request_body(reference_ids=None, target_id="sweep-2"):
    return {"reference_ids": ["sweep-0", "sweep-1"] if reference_ids is None else reference_ids,
            "target_id": target_id}


def incompatible_changes():
    return ({"session_id": "other"}, {"stream_id": "other"},
            {"frequencies_hz": [100, 200, 300, 401]}, {"level_unit": "dB"},
            {"level_unit": "s_meter_count"}, {"settings": {}}, {"settings": {"sequence": 1}},
            {"settings": {"rbw_hz": None}}, {"settings": {"extension": [None]}},
            {"settings": {"rbw_hz": 1}}, {"settings": {"rbw_hz": True}},
            {"settings": {"rbw_hz": "10000"}}, {"settings": {"rbw_hz": 10000, "extra": 1}},
            {"settings": {"rbw_hz": 10000, "sequence": float("nan")}})


class AverageArithmeticTests(unittest.TestCase):
    def calculate(self, references=(-100, -90), target=-80, frequencies=(100,), **changes):
        sweeps = [Sweep.from_dict(measurement(number=i, frequencies_hz=frequencies,
                    levels=[value] * len(frequencies), **changes)) for i, value in enumerate(references)]
        target_sweep = Sweep.from_dict(measurement(number=len(references), frequencies_hz=frequencies,
                    levels=[target] * len(frequencies), **changes))
        return calculate_sweep_average_comparison("session", sweeps, target_sweep)

    def test_linear_power_mean_not_arithmetic_dbm(self):
        result = self.calculate()
        self.assertAlmostEqual(result["reference"]["levels"][0], -92.59637310505756)
        self.assertNotEqual(result["reference"]["levels"], [-95])
        self.assertAlmostEqual(result["delta_levels"][0], 12.59637310505756)
        self.assertEqual(result["reference"]["method"], "mean_power")
        self.assertEqual(result["reference"]["count"], 2)
        self.assertEqual(result["reference"]["level_unit"], "dBm")
        self.assertEqual(result["delta_unit"], "dB")
        self.assertEqual(result["points"], 1)

    def test_provenance_order_full_settings_no_fake_id_or_raw_reference_levels(self):
        settings = {"rbw_hz": 10000, "extension": {"values": [1, "two"]}}
        first = Sweep.from_dict(measurement(number=1, levels=[-100] * 4,
                    settings=dict(settings, sequence=1), interval={
                        "started_at": measurement_interval(1)["started_at"],
                        "ended_at": measurement_interval(30)["ended_at"]}))
        second = Sweep.from_dict(measurement(number=20, levels=[-90] * 4,
                    settings=dict(settings, sequence=None)))
        target = Sweep.from_dict(measurement(number=0, settings=dict(settings, sequence=0)))
        originals = copy.deepcopy((first, second, target))
        result = calculate_sweep_average_comparison("session", [second, first], target)
        ref = result["reference"]
        self.assertEqual(ref["source_ids"], [second.id, first.id])
        self.assertEqual(ref["interval"], to_primitive(first.interval))
        for source, sweep in zip(ref["sources"], (second, first)):
            self.assertEqual(source, {"id": sweep.id, "interval": to_primitive(sweep.interval),
                                      "settings": sweep.settings})
        self.assertNotIn("id", ref)
        self.assertNotIn("settings", ref)
        self.assertAlmostEqual(ref["levels"][0], -92.59637310505756)  # No duration weighting.
        self.assertEqual(result["target"]["interval"], to_primitive(target.interval))
        ref["sources"][0]["settings"]["extension"]["values"].append(3)
        ref["levels"][0] = 2
        result["target"]["settings"]["extension"]["values"].append(4)
        result["target"]["levels"][0] = 2
        result["frequencies_hz"][0] = 0
        self.assertEqual((first, second, target), originals)

    def test_signed_extrema_first_tie_duplicate_bins_and_zero(self):
        refs = [Sweep.from_dict(measurement(number=i, frequencies_hz=[100, 100, 200, 200])) for i in range(2)]
        for deltas in ([5, -5, 5, -5], [-1, -2, -3, -4], [1, 2, 3, 4], [0] * 4):
            target = Sweep.from_dict(measurement(number=2, frequencies_hz=[100, 100, 200, 200],
                        levels=[level + delta for level, delta in zip(refs[0].levels, deltas)]))
            result = calculate_sweep_average_comparison("session", refs, target)
            self.assertEqual(result["delta_levels"], deltas)
            self.assertEqual(result["frequencies_hz"], [100, 100, 200, 200])
            for key, value in (("max_increase", max(deltas)), ("min_change", min(deltas))):
                self.assertEqual(result[key], {"frequency_hz": target.frequencies_hz[deltas.index(value)],
                                               "delta_db": value})

    def test_extreme_power_underflow_overflow_and_finite_delta(self):
        largest, tiny = sys.float_info.max, math.ulp(0.0)
        for level in (-4000, 4000, -largest, largest, tiny, -tiny):
            result = self.calculate((level,) * 16, level)
            self.assertEqual(result["reference"]["levels"], [level])
            self.assertEqual(result["delta_levels"], [0])
            json.dumps(result, allow_nan=False)
        for levels, expected in (((-4000, -4010), -4002.5963731050576),
                                 ((4000, 3990), 3997.4036268949424),
                                 ((-4000, 4000), 4000 - 10 * math.log10(2))):
            result = self.calculate(levels, expected)
            self.assertAlmostEqual(result["reference"]["levels"][0], expected)
        # Pairwise subtraction would overflow, but the requested mean comparison is finite.
        result = self.calculate((-largest, largest), largest)
        self.assertEqual(result["reference"]["levels"], [largest])
        self.assertEqual(result["delta_levels"], [0])
        for references, target in (((-largest, -largest), largest), ((largest, largest), -largest)):
            with self.assertRaises(AverageReferenceIncompatibleError):
                self.calculate(references, target)

    def test_adjacent_doubles_keep_mean_within_source_range(self):
        low, high = -2.591576022265827, -2.5915760222658264
        levels = [low] * 9 + [high]
        result = self.calculate(levels, high)
        mean = result["reference"]["levels"][0]
        self.assertGreaterEqual(mean, low)
        self.assertLessEqual(mean, high)
        self.assertEqual(mean, low)
        for value in (-100, -1, 0, 1, 100):
            for direction in (-math.inf, math.inf):
                neighbor = math.nextafter(value, direction)
                mean = self.calculate([value] * 15 + [neighbor], value)["reference"]["levels"][0]
                self.assertLessEqual(min(value, neighbor), mean)
                self.assertLessEqual(mean, max(value, neighbor))

    def test_count_points_and_safe_integer_frequency_boundaries(self):
        for count in (2, 16):
            for points in (1, MAX_COMPARISON_POINTS):
                result = self.calculate((-80,) * count, -70, tuple(range(points)))
                self.assertEqual(result["reference"]["count"], count)
                self.assertEqual(len(result["delta_levels"]), points)
                self.assertEqual(result["points"], points)
        with self.assertRaises(AverageReferenceIncompatibleError):
            self.calculate(frequencies=tuple(range(MAX_COMPARISON_POINTS + 1)))
        self.assertEqual(self.calculate(frequencies=(MAX_FREQUENCY_HZ,))["frequencies_hz"], [MAX_FREQUENCY_HZ])
        with self.assertRaises(AverageReferenceIncompatibleError):
            self.calculate(frequencies=(MAX_FREQUENCY_HZ + 1,))
        for count in (0, 1, 17):
            with self.assertRaises(ValueError):
                self.calculate((-80,) * count)

    def test_every_source_and_target_use_shared_compatibility_gate(self):
        for position in range(3):
            for changes in incompatible_changes():
                with self.subTest(position=position, changes=changes):
                    sweeps = [Sweep.from_dict(measurement(number=i, **(changes if i == position else {})))
                              for i in range(3)]
                    with self.assertRaises(AverageReferenceIncompatibleError):
                        calculate_sweep_average_comparison("session", sweeps[:2], sweeps[2])
        for levels in ((), (1,), (float("inf"),) * 4, (float("nan"),) * 4):
            sweeps = [Sweep.from_dict(measurement(number=i)) for i in range(3)]
            sweeps[1].levels = levels
            with self.assertRaises(AverageReferenceIncompatibleError):
                calculate_sweep_average_comparison("session", sweeps[:2], sweeps[2])
        sweeps = [Sweep.from_dict(measurement(number=i)) for i in range(3)]
        for sweep in sweeps:
            sweep.levels = sweep.frequencies_hz = ()
        with self.assertRaises(AverageReferenceIncompatibleError):
            calculate_sweep_average_comparison("session", sweeps[:2], sweeps[2])

    def test_request_validation_distinct_separate_exact_ids_and_unicode_limit(self):
        valid = request_body()
        invalid = [None, [], True, {}, dict(valid, extra=1), {"reference_ids": []}, {"target_id": "x"}]
        invalid += [request_body(value) for value in ("ab", True, {}, (), [], ["a"], [str(i) for i in range(17)], ["a", "a"])]
        invalid += [request_body(["a", value]) for value in (None, True, 1, "", " ", {}, [], "x" * 129, "\ud800")]
        invalid += [request_body(target_id=value) for value in (None, True, 1, "", " ", {}, [], "x" * 129, "\ud800", "sweep-0")]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_average_request(payload)
        exact = request_body([" padded /+ id ", "😀" * 128], "x" * 128)
        self.assertEqual(validate_average_request(exact), (exact["reference_ids"], exact["target_id"]))
        self.assertEqual(validate_average_request(request_body(["a", " a "], "a ")),
                         (["a", " a "], "a "))


class AverageServiceMixin:
    def add(self, number=0, **changes):
        return self.service.ingest_sweep(measurement(self.session.id, self.stream_id, number, **changes))

    def calculate(self, references=None, target="sweep-2"):
        return self.service.sweep_average_comparison(self.session.id, request_body(references, target))

    def snapshot(self):
        return copy.deepcopy((self.service.health(), self.service.events(), self.service.list_sessions(),
                              self.service.list_sweeps(self.session.id), self.service.archive_capacity(),
                              self.service.list_sweep_references(self.session.id),
                              self.service.list_candidates(self.session.id), self.service.list_investigations(self.session.id)))

    def test_old_exact_ids_outside_recent_list_in_one_lock(self):
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
            result = self.calculate(["sweep-1", "sweep-0"])
        self.assertEqual(seen, ["sweep-1", "sweep-0", "sweep-2"])
        self.assertEqual(result["reference"]["source_ids"], seen[:2])
        self.assertEqual(result["delta_levels"], [0] * 4)

    def test_success_refusal_and_response_edits_leave_sources_state_events_unchanged(self):
        for i in range(3):
            self.add(i, settings={"rbw_hz": 100, "extension": {"a": [1]}, "sequence": i})
        self.add(3, settings={})
        before = self.snapshot()
        subscriber_id, subscriber = self.service.subscribe(after=before[0]["last_event_id"])
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        result = self.calculate()
        result["reference"]["sources"][0]["settings"]["extension"]["a"].append(2)
        result["reference"]["levels"][0] = 100
        result["target"]["levels"][0] = 100
        result["frequencies_hz"][0] = 999
        with self.assertRaises(AverageReferenceIncompatibleError):
            self.calculate(target="sweep-3")
        with self.assertRaises(ValueError):
            self.calculate(["sweep-0", "sweep-0"])
        with self.assertRaises(SweepNotFoundError):
            self.calculate(target="missing")
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        self.add(4)
        self.assertEqual(self.service.health()["last_event_id"], before[0]["last_event_id"] + 1)

    def test_exact_missing_foreign_stream_ids_and_capability(self):
        for i in range(3):
            self.add(i)
        self.add(3, id=" encoded/+ sweep ")
        self.assertEqual(self.calculate(target=" encoded/+ sweep ")["target"]["id"], " encoded/+ sweep ")
        second = create_session(self.service)
        self.service.ingest_sweep(measurement(second.id, second.streams[0].id, 4))
        self.service.ingest_sweep(measurement(self.session.id, self.session.streams[1].id, 5))
        for missing in ("missing", "sweep-4", "encoded/+ sweep"):
            with self.assertRaises(SweepNotFoundError):
                self.calculate(target=missing)
            with self.assertRaises(SweepNotFoundError):
                self.calculate(["sweep-0", missing])
        with self.assertRaises(AverageReferenceIncompatibleError):
            self.calculate(target="sweep-5")
        with self.assertRaises(SessionNotFoundError):
            self.service.sweep_average_comparison("missing", request_body())
        self.assertTrue(self.service.capabilities()["features"]["averaged_sweep_reference"])


class MemoryAverageTests(AverageServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteMemoryAverageTests(AverageServiceMixin, unittest.TestCase):
    def setUp(self):
        self.store = SQLiteSessionStore(":memory:")
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteAverageTests(AverageServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(lambda: self.service.close())
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id

    def test_only_primary_key_reads_no_changes_logical_dump_schema_or_scan(self):
        for i in range(3):
            self.add(i)
        self.add(3, settings={})
        connection = self.store._connection
        before = (connection.total_changes, list(connection.iterdump()))
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            with patch.object(self.store, "list_sweeps", side_effect=AssertionError("no archive scan")):
                self.calculate()
                with self.assertRaises(AverageReferenceIncompatibleError):
                    self.calculate(target="sweep-3")
                with self.assertRaises(ValueError):
                    self.calculate(["sweep-0"])
                with self.assertRaises(SweepNotFoundError):
                    self.calculate(target="missing")
        finally:
            connection.set_trace_callback(None)
        self.assertEqual((connection.total_changes, list(connection.iterdump())), before)
        self.assertEqual(len(statements), 9)
        self.assertTrue(all(statement.startswith("SELECT * FROM sweeps WHERE id = ") for statement in statements))
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM sweeps WHERE id = ?", ("sweep-0",)).fetchall()
        self.assertTrue(any("SEARCH sweeps USING INDEX" in row[3] for row in plan))

    def test_restart_capacity_full_old_sources_still_readable(self):
        self.test_old_exact_ids_outside_recent_list_in_one_lock()
        expected = self.calculate()
        size = self.service.archive_capacity()["database_bytes"]
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=size)
        self.service = SessionService(store=self.store, history_limit=2)
        self.assertEqual(self.service.archive_capacity()["state"], "full")
        before = list(self.store._connection.iterdump())
        self.assertEqual(self.calculate(), expected)
        self.assertEqual(list(self.store._connection.iterdump()), before)


class AverageHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(path))
        self.session = create_session(service)
        stream = self.session.streams[0].id
        service.ingest_sweeps([
            *(measurement(self.session.id, stream, number, levels=[level] * 4)
              for number, level in enumerate((-100, -90, -80))),
            measurement(self.session.id, stream, 3, settings={}),
            measurement(self.session.id, stream, 4, id=" encoded/+ sweep "),
        ])
        second = create_session(service)
        service.ingest_sweeps([
            measurement(second.id, second.streams[0].id, 5),
            *(measurement(self.session.id, stream, number, levels=[level] * 4)
              for number, level in ((6, -sys.float_info.max), (7, -sys.float_info.max), (8, sys.float_info.max))),
        ])
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/sweep-average-comparison"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, payload=None, *, raw=None, path=None, method="POST"):
        if raw is None and method == "POST":
            raw = json.dumps(request_body() if payload is None else payload).encode()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path or self.root, body=raw,
                               headers={"Content-Type": "application/json"} if raw is not None else {})
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

    def test_success_at_capacity_envelope_exact_id_and_capability(self):
        service = self.server.session_service
        before = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity(),
                  list(service._store._connection.iterdump()))
        status, envelope = self.request(request_body(["sweep-1", "sweep-0"]))
        self.assertEqual(status, 200)
        self.assertEqual(envelope["contract_version"], 1)
        self.assertEqual(envelope["data"]["reference"]["source_ids"], ["sweep-1", "sweep-0"])
        self.assertAlmostEqual(envelope["data"]["delta_levels"][0], 12.59637310505756)
        after = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity(),
                 list(service._store._connection.iterdump()))
        self.assertEqual(before, after)
        self.assertEqual(after[3]["state"], "full")
        status, capability = self.request(path="/api/v1/capabilities", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(capability["data"]["features"]["averaged_sweep_reference"])
        status, response = self.request(request_body(target_id=" encoded/+ sweep "))
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["target"]["id"], " encoded/+ sweep ")

    def test_strict_body_malformed_utf8_duplicate_keys_and_invalid_types(self):
        valid = request_body()
        invalid = [b"", b"{", b"[]", b"null", b"true", b"{}", b"\xff", b"\xef\xbb\xbf{}",
                   json.dumps(dict(valid, extra=1)).encode(),
                   json.dumps({"reference_ids": ["sweep-0", "sweep-1"]}).encode(),
                   json.dumps({"target_id": "sweep-2"}).encode(),
                   b"[" * 1200 + b"0" + b"]" * 1200]
        for extra in ('"reference_ids": ["sweep-0", "sweep-1"]', '"target_id": "sweep-2"',
                      '"extra": {"x": 1, "x": 2}', '"extra": NaN', '"extra": Infinity',
                      '"extra": 1e999', '"extra": "\\ud800"'):
            invalid.append((json.dumps(valid)[:-1] + ', ' + extra + '}').encode())
        for value in (None, True, 10, {}, "ab", [], ["a"], ["a", "a"], ["sweep-0", "sweep-2"],
                      ["sweep-0", "x" * 129], ["sweep-0", "\ud800"], ["sweep-0", " "],
                      [str(i) for i in range(17)]):
            invalid.append(json.dumps(dict(valid, reference_ids=value)).encode())
        for value in (None, True, 10, "", "  ", "\ud800", "x" * 129, "sweep-0"):
            invalid.append(json.dumps(dict(valid, target_id=value)).encode())
        before = self.server.session_service.health()
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                status, response = self.request(raw=raw)
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.server.session_service.health(), before)

    def test_no_query_even_bare_question_mark_and_get_unsupported(self):
        for suffix in ("?", "?x=1", "?x=", "?&", "?target_id=sweep-2"):
            status, response = self.request(path=self.root + suffix)
            self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.request(method="GET")[0], 404)

    def test_missing_foreign_incompatible_and_overflow_error_mapping(self):
        for payload, expected, code in (
            (request_body(target_id="missing"), 404, "sweep_not_found"),
            (request_body(target_id="sweep-5"), 404, "sweep_not_found"),
            (request_body(["sweep-0", "sweep-5"]), 404, "sweep_not_found"),
            (request_body(["sweep-0", "missing"]), 404, "sweep_not_found"),
            (request_body(target_id="sweep-3"), 409, "average_reference_incompatible"),
            (request_body(["sweep-0", "sweep-3"]), 409, "average_reference_incompatible"),
            (request_body(["sweep-6", "sweep-7"], "sweep-8"), 409, "average_reference_incompatible"),
        ):
            status, response = self.request(payload)
            self.assertEqual((status, response["error"]["code"]), (expected, code))
        status, response = self.request(path="/api/v1/sessions/missing/sweep-average-comparison")
        self.assertEqual((status, response["error"]["code"]), (404, "session_not_found"))

    def test_size_bound_before_read_exact_boundary_and_leading_zero_length(self):
        for size in (str(MAX_AVERAGE_REQUEST_BYTES + 1), "9" * 5000):
            status, response = self.raw_headers(f"Content-Length: {size}\r\n")
            self.assertEqual((status, response["error"]["code"]), (413, "request_too_large"))
        raw = json.dumps(request_body()).encode()
        self.assertEqual(self.request(raw=raw + b" " * (MAX_AVERAGE_REQUEST_BYTES - len(raw)))[0], 200)
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
