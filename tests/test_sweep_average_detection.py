"""Synthetic-only tests for read-only averaged-sweep change detection."""

from __future__ import annotations

import copy
import http.client
import math
import queue
import socket
import sys
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from radioinspector.contracts import Sweep, api_payload
from radioinspector.jsonio import dumps as json_dumps
from radioinspector.session_service import SessionNotFoundError, SessionService, SweepNotFoundError
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.sweep_average import (
    MAX_AVERAGE_REQUEST_BYTES, MAX_CHANGE_DETECTION_RESPONSE_BYTES, MAX_FREQUENCY_HZ,
    AverageReferenceIncompatibleError,
    calculate_sweep_average_change_detection,
    calculate_sweep_average_comparison,
    validate_change_detection_request,
)
from radioinspector.sweep_comparison import MAX_COMPARISON_POINTS
from radioinspector.web.server import create_server
from tests.test_sweep_average import incompatible_changes
from tests.test_sweep_comparison import create_session, measurement


def _request(**changes):
    payload = {
        "reference_ids": ["sweep-0", "sweep-1"],
        "target_id": "sweep-2",
        "minimum_increase_db": 6,
        "minimum_bins": 2,
    }
    payload.update(changes)
    return payload


class AverageChangeDetectionTests(unittest.TestCase):
    def test_detects_runs_and_preserves_duplicate_frequency_bins(self):
        baseline = [-100] * 9
        deltas = [0, 6, 9, 10, 10, 1, 7, 8, 2]
        frequencies = [100, 200, 200, 300, 400, 500, 600, 700, 800]
        refs = [
            measurement(number=i, frequencies_hz=frequencies, levels=baseline)
            for i in range(2)
        ]
        target = measurement(number=2, frequencies_hz=frequencies,
                             levels=[base + delta for base, delta in zip(baseline, deltas)])
        result = calculate_sweep_average_change_detection(
            "session", [Sweep.from_dict(item) for item in refs], Sweep.from_dict(target),
            minimum_increase_db=6, minimum_bins=2,
        )
        self.assertEqual(result["comparison"]["delta_levels"], deltas)
        self.assertEqual(result["detection"], {
            "minimum_increase_db": 6.0,
            "minimum_bins": 2,
            "count": 2,
            "regions": [
                {"start_index": 1, "end_index": 4, "start_frequency_hz": 200,
                 "end_frequency_hz": 400, "peak_index": 3, "peak_frequency_hz": 300,
                 "peak_increase_db": 10, "bin_count": 4},
                {"start_index": 6, "end_index": 7, "start_frequency_hz": 600,
                 "end_frequency_hz": 700, "peak_index": 7, "peak_frequency_hz": 700,
                 "peak_increase_db": 8, "bin_count": 2},
            ],
        })

    def test_validation_requires_exact_fields_and_bounded_values(self):
        self.assertEqual(validate_change_detection_request(_request()),
                         (["sweep-0", "sweep-1"], "sweep-2", 6.0, 2))
        invalid = [
            _request(extra=True),
            _request(minimum_increase_db=True),
            _request(minimum_increase_db=float("nan")),
            _request(minimum_increase_db=float("inf")),
            _request(minimum_increase_db=10**1000),
            _request(minimum_increase_db=-1),
            _request(minimum_increase_db=121),
            _request(minimum_bins=True),
            _request(minimum_bins=0),
            _request(minimum_bins=16_385),
            _request(minimum_bins=2.0),
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_change_detection_request(payload)


    def calculate(self, deltas, *, threshold=6, bins=1, references=2, frequencies=None):
        frequencies = list(range(len(deltas))) if frequencies is None else frequencies
        sweeps = [Sweep.from_dict(measurement(
            number=i, frequencies_hz=frequencies, levels=[0] * len(deltas),
        )) for i in range(references)]
        target = Sweep.from_dict(measurement(
            number=references, frequencies_hz=frequencies, levels=deltas,
        ))
        return calculate_sweep_average_change_detection(
            "session", sweeps, target, minimum_increase_db=threshold, minimum_bins=bins,
        )

    def test_maximal_runs_edges_gaps_inclusive_threshold_and_first_peak_ties(self):
        for deltas, threshold, bins, expected in (
            ([6, 7, 0, 6], 6, 1, [(0, 1, 1), (3, 3, 3)]),
            ([6, 6, 0, 6, 6, 6], 6, 2, [(0, 1, 0), (3, 5, 3)]),
            ([6, 0, 6, 6, 0, 6], 6, 2, [(2, 3, 2)]),
            ([7, 7], 6, 3, []),
            ([-1, -2, -3], 0, 1, []),
            ([0, 0, -1, 0], 0, 1, [(0, 1, 0), (3, 3, 3)]),
            ([120, math.nextafter(120, 0), math.nextafter(120, math.inf)],
             120, 1, [(0, 0, 0), (2, 2, 2)]),
        ):
            with self.subTest(deltas=deltas, threshold=threshold, bins=bins):
                result = self.calculate(deltas, threshold=threshold, bins=bins)
                regions = result["detection"]["regions"]
                self.assertEqual([(r["start_index"], r["end_index"], r["peak_index"])
                                  for r in regions], expected)
                self.assertEqual(result["detection"]["count"], len(expected))
                for region, (start, end, peak) in zip(regions, expected):
                    self.assertEqual(region["bin_count"], end - start + 1)
                    self.assertEqual(region["peak_increase_db"], deltas[peak])
                    self.assertEqual(region["peak_frequency_hz"], peak)
                json.dumps(result, allow_nan=False)

    def test_full_point_and_reference_limits_no_truncation(self):
        result = self.calculate([120] * MAX_COMPARISON_POINTS, threshold=120,
                                bins=MAX_COMPARISON_POINTS, references=16)
        self.assertEqual(result["comparison"]["reference"]["count"], 16)
        self.assertEqual(result["comparison"]["points"], MAX_COMPARISON_POINTS)
        region, = result["detection"]["regions"]
        self.assertEqual((region["start_index"], region["end_index"], region["peak_index"],
                          region["bin_count"]), (0, MAX_COMPARISON_POINTS - 1, 0, MAX_COMPARISON_POINTS))
        result = self.calculate([6, 0] * (MAX_COMPARISON_POINTS // 2))
        self.assertEqual(result["detection"]["count"], MAX_COMPARISON_POINTS // 2)
        self.assertEqual(result["detection"]["regions"][-1]["start_index"], MAX_COMPARISON_POINTS - 2)
        for deltas, frequencies in (([1] * (MAX_COMPARISON_POINTS + 1), None),
                                   ([1], [MAX_FREQUENCY_HZ + 1])):
            with self.assertRaises(AverageReferenceIncompatibleError):
                self.calculate(deltas, frequencies=frequencies)
        self.assertEqual(self.calculate([6], frequencies=[MAX_FREQUENCY_HZ])["detection"]
                         ["regions"][0]["peak_frequency_hz"], MAX_FREQUENCY_HZ)

    def test_shared_mean_power_arithmetic_provenance_and_overflow_gate(self):
        references = [Sweep.from_dict(measurement(number=i, levels=[level] * 4))
                      for i, level in ((20, -100), (10, -90))]
        target = Sweep.from_dict(measurement(number=0, levels=[-80] * 4))
        sources = copy.deepcopy((references, target))
        result = calculate_sweep_average_change_detection(
            "session", references, target, minimum_increase_db=13, minimum_bins=1,
        )
        self.assertEqual(result["comparison"], calculate_sweep_average_comparison("session", references, target))
        self.assertAlmostEqual(result["comparison"]["delta_levels"][0], 12.59637310505756)
        self.assertEqual(result["detection"]["regions"], [])  # A dBm mean would wrongly detect 15 dB.
        self.assertEqual((references, target), sources)
        largest = sys.float_info.max
        for level in (-largest, largest, -4000, 4000, math.ulp(0.0)):
            for sweep in (*references, target):
                sweep.levels = (level,) * 4
            result = calculate_sweep_average_change_detection(
                "session", references, target, minimum_increase_db=0, minimum_bins=4,
            )
            self.assertEqual(result["comparison"]["delta_levels"], [0] * 4)
            self.assertEqual(result["detection"]["regions"][0]["bin_count"], 4)
            json.dumps(result, allow_nan=False)
        for sweep in references:
            sweep.levels = (-largest,) * 4
        target.levels = (largest,) * 4
        with self.assertRaises(AverageReferenceIncompatibleError):
            calculate_sweep_average_change_detection(
                "session", references, target, minimum_increase_db=6, minimum_bins=1,
            )

    def test_response_byte_limit_realistic_frequency_regions_without_truncation(self):
        count = MAX_COMPARISON_POINTS
        frequencies = list(range(100_000_000, 100_000_000 + count * 1000, 1000))
        references = [Sweep.from_dict(measurement(number=i, frequencies_hz=frequencies,
                      levels=[-100] * count)) for i in range(16)]
        target = Sweep.from_dict(measurement(number=16, frequencies_hz=frequencies,
                                 levels=[-94, -100] * (count // 2)))
        with patch("radioinspector.sweep_average.MAX_CHANGE_DETECTION_RESPONSE_BYTES", 3_000_000):
            result = calculate_sweep_average_change_detection(
                "session", references, target, minimum_increase_db=6, minimum_bins=1,
            )
        self.assertEqual(result["detection"]["count"], 8192)
        self.assertEqual(len(json_dumps(api_payload(result)).encode("utf-8")), 2_157_684)
        self.assertGreater(2_157_684, MAX_CHANGE_DETECTION_RESPONSE_BYTES)
        before = copy.deepcopy((references, target))
        with self.assertRaisesRegex(AverageReferenceIncompatibleError, "2000000.*не усечены"):
            calculate_sweep_average_change_detection(
                "session", references, target, minimum_increase_db=6, minimum_bins=1,
            )
        self.assertEqual((references, target), before)
        # The existing comparison remains available, with every original bin.
        self.assertEqual(calculate_sweep_average_comparison("session", references, target)["points"], count)
        result = calculate_sweep_average_change_detection(
            "session", references, target, minimum_increase_db=6, minimum_bins=2,
        )
        self.assertEqual(result["comparison"]["points"], count)
        self.assertEqual(result["detection"]["count"], 0)

    def test_response_limit_uses_complete_envelope_utf8_bytes_and_accepts_exact_boundary(self):
        references = [Sweep.from_dict(measurement(number=i)) for i in range(2)]
        references[0].settings["sequence"] = ""
        target = Sweep.from_dict(measurement(number=2))
        kwargs = {"minimum_increase_db": 0, "minimum_bins": 1}
        result = calculate_sweep_average_change_detection("session", references, target, **kwargs)
        base_size = len(json_dumps(api_payload(result)).encode("utf-8"))
        padding = MAX_CHANGE_DETECTION_RESPONSE_BYTES - base_size
        # sequence is provenance, ignored only when comparing acquisition settings.
        references[0].settings["sequence"] = "я" * (padding // 2) + "x" * (padding % 2)
        result = calculate_sweep_average_change_detection("session", references, target, **kwargs)
        encoded = json_dumps(api_payload(result))
        self.assertLess(len(encoded), MAX_CHANGE_DETECTION_RESPONSE_BYTES)
        self.assertEqual(len(encoded.encode("utf-8")), MAX_CHANGE_DETECTION_RESPONSE_BYTES)
        references[0].settings["sequence"] += "x"
        with self.assertRaisesRegex(AverageReferenceIncompatibleError, "2000000.*не усечены"):
            calculate_sweep_average_change_detection("session", references, target, **kwargs)
        # This cap belongs only to the new detector, not the older comparison.
        comparison = calculate_sweep_average_comparison("session", references, target)
        self.assertEqual(comparison["reference"]["sources"][0]["settings"]["sequence"],
                         references[0].settings["sequence"])

    def test_all_sources_must_pass_compatibility_before_detection(self):
        for position in range(3):
            for changes in incompatible_changes():
                with self.subTest(position=position, changes=changes):
                    sweeps = [Sweep.from_dict(measurement(number=i, **(changes if i == position else {})))
                              for i in range(3)]
                    with self.assertRaises(AverageReferenceIncompatibleError):
                        calculate_sweep_average_change_detection(
                            "session", sweeps[:2], sweeps[2], minimum_increase_db=6, minimum_bins=1,
                        )

    def test_direct_parameters_and_exact_request_shape(self):
        for key in _request():
            payload = _request()
            del payload[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_change_detection_request(payload)
        for payload in (None, [], True, 1, "{}", {}, _request(reference_ids=["a", "a"]),
                        _request(target_id="sweep-0"), _request(target_id="\ud800")):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_change_detection_request(payload)
        for threshold in (None, "6", True, [], {}, -1, 121, 10**1000,
                          float("nan"), float("inf"), -float("inf")):
            with self.subTest(threshold=threshold):
                with self.assertRaises(ValueError):
                    self.calculate([10], threshold=threshold)
                with self.assertRaises(ValueError):
                    validate_change_detection_request(_request(minimum_increase_db=threshold))
        for bins in (None, "1", True, [], {}, 0, -1, 1.0, 16_385, 10**1000):
            with self.subTest(bins=bins):
                with self.assertRaises(ValueError):
                    self.calculate([10], bins=bins)
                with self.assertRaises(ValueError):
                    validate_change_detection_request(_request(minimum_bins=bins))
        for threshold, bins in ((0, 1), (120, MAX_COMPARISON_POINTS)):
            self.assertEqual(validate_change_detection_request(_request(
                minimum_increase_db=threshold, minimum_bins=bins,
            ))[-2:], (float(threshold), bins))


class AverageChangeDetectionServiceMixin:
    def add(self, number=0, **changes):
        return self.service.ingest_sweep(measurement(self.session.id, self.stream_id, number, **changes))

    def calculate(self, **changes):
        return self.service.sweep_average_change_detection(self.session.id, _request(**changes))

    def snapshot(self):
        return copy.deepcopy((self.service.health(), self.service.events(), self.service.list_sessions(),
                              self.service.list_sweeps(self.session.id), self.service.archive_capacity(),
                              self.service.list_sweep_references(self.session.id),
                              self.service.list_candidates(self.session.id),
                              self.service.list_investigations(self.session.id)))

    def test_exact_old_sources_under_one_lock_without_recent_list_scans(self):
        for start, end in ((0, 100), (100, 112)):
            self.service.ingest_sweeps([measurement(self.session.id, self.stream_id, i)
                                       for i in range(start, end)])
        self.assertNotIn("sweep-0", [s.id for s in self.service.list_sweeps(self.session.id)])
        reader = self.service._get_sweep_locked
        seen = []

        def checked_reader(session_id, sweep_id):
            self.assertTrue(self.service._lock._is_owned())
            seen.append(sweep_id)
            return reader(session_id, sweep_id)

        with patch.object(self.service, "_get_sweep_locked", side_effect=checked_reader), \
                patch.object(self.service, "list_sweeps", side_effect=AssertionError("no archive scan")):
            result = self.calculate(reference_ids=["sweep-1", "sweep-0"])
        self.assertEqual(seen, ["sweep-1", "sweep-0", "sweep-2"])
        self.assertEqual(result["comparison"]["reference"]["source_ids"], seen[:2])
        self.assertEqual(result["detection"]["regions"], [])

    def test_success_errors_and_edited_result_do_not_mutate_sources_or_emit_events(self):
        for i in range(3):
            self.add(i, settings={"rbw_hz": 100, "extension": {"a": [1]}, "sequence": i})
        self.add(3, settings={})
        before = self.snapshot()
        subscriber_id, subscriber = self.service.subscribe(after=before[0]["last_event_id"])
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        result = self.calculate(minimum_increase_db=0)
        result["comparison"]["reference"]["sources"][0]["settings"]["extension"]["a"].append(2)
        result["comparison"]["reference"]["levels"][0] = 100
        result["comparison"]["target"]["levels"][0] = 100
        result["comparison"]["frequencies_hz"][0] = 999
        result["detection"]["regions"][0]["start_index"] = 999
        with self.assertRaises(AverageReferenceIncompatibleError):
            self.calculate(target_id="sweep-3")
        with self.assertRaises(ValueError):
            self.calculate(minimum_bins=True)
        with self.assertRaises(SweepNotFoundError):
            self.calculate(target_id="missing")
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        self.add(4)
        self.assertEqual(self.service.health()["last_event_id"], before[0]["last_event_id"] + 1)

    def test_exact_foreign_and_cross_stream_ids(self):
        for i in range(3):
            self.add(i)
        self.add(3, id=" encoded/+ sweep ")
        self.assertEqual(self.calculate(target_id=" encoded/+ sweep ")["comparison"]["target"]["id"],
                         " encoded/+ sweep ")
        second = create_session(self.service)
        self.service.ingest_sweep(measurement(second.id, second.streams[0].id, 4))
        self.service.ingest_sweep(measurement(self.session.id, self.session.streams[1].id, 5))
        for missing in ("missing", "sweep-4", "encoded/+ sweep"):
            with self.assertRaises(SweepNotFoundError):
                self.calculate(target_id=missing)
            with self.assertRaises(SweepNotFoundError):
                self.calculate(reference_ids=["sweep-0", missing])
        with self.assertRaises(AverageReferenceIncompatibleError):
            self.calculate(target_id="sweep-5")
        with self.assertRaises(SessionNotFoundError):
            self.service.sweep_average_change_detection("missing", _request())
        self.assertTrue(self.service.capabilities()["features"]["averaged_sweep_change_detection"])


class MemoryAverageChangeDetectionTests(AverageChangeDetectionServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteAverageChangeDetectionTests(AverageChangeDetectionServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(lambda: self.service.close())
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id

    def test_primary_key_reads_only_and_no_sqlite_changes(self):
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
                    self.calculate(target_id="sweep-3")
                with self.assertRaises(ValueError):
                    self.calculate(minimum_bins=True)
                with self.assertRaises(SweepNotFoundError):
                    self.calculate(target_id="missing")
        finally:
            connection.set_trace_callback(None)
        self.assertEqual((connection.total_changes, list(connection.iterdump())), before)
        self.assertEqual(len(statements), 9)
        self.assertTrue(all(statement.startswith("SELECT * FROM sweeps WHERE id = ") for statement in statements))
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM sweeps WHERE id = ?", ("sweep-0",)).fetchall()
        self.assertTrue(any("SEARCH sweeps USING INDEX" in row[3] for row in plan))

    def test_restart_and_full_archive_keep_old_sources_readable(self):
        self.test_exact_old_sources_under_one_lock_without_recent_list_scans()
        expected = self.calculate()
        size = self.service.archive_capacity()["database_bytes"]
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=size)
        self.service = SessionService(store=self.store, history_limit=2)
        self.assertEqual(self.service.archive_capacity()["state"], "full")
        before = (self.store._connection.total_changes, list(self.store._connection.iterdump()))
        self.assertEqual(self.calculate(), expected)
        self.assertEqual((self.store._connection.total_changes, list(self.store._connection.iterdump())), before)


class AverageChangeDetectionHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(path))
        self.session = create_session(service)
        stream_id = self.session.streams[0].id
        levels = ([-100] * 4, [-100] * 4, [-100, -90, -80, -70])
        for number, sweep_levels in enumerate(levels):
            service.ingest_sweep(measurement(
                self.session.id, stream_id, number, levels=sweep_levels,
            ))
        service.ingest_sweep(measurement(self.session.id, stream_id, 3, settings={}))
        service.ingest_sweep(measurement(self.session.id, stream_id, 4, id=" encoded/+ sweep "))
        second = create_session(service)
        service.ingest_sweep(measurement(second.id, second.streams[0].id, 5))
        for number, level in ((6, -sys.float_info.max), (7, -sys.float_info.max), (8, sys.float_info.max)):
            service.ingest_sweep(measurement(self.session.id, stream_id, number, levels=[level] * 4))
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.path = f"/api/v1/sessions/{self.session.id}/sweep-average-change-detection"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, payload=None, path=None, raw=None, method="POST"):
        body = raw if raw is not None else json.dumps(
            _request() if payload is None else payload,
        ).encode()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path or self.path, body=body,
                              headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_result_is_read_only_and_advertised(self):
        service = self.server.session_service
        before = (service.health(), service.events(), list(service._store._connection.iterdump()))
        status, response = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(response["contract_version"], 1)
        self.assertEqual(response["data"]["detection"]["regions"], [
            {"start_index": 1, "end_index": 3, "start_frequency_hz": 200,
             "end_frequency_hz": 400, "peak_index": 3, "peak_frequency_hz": 400,
             "peak_increase_db": 30, "bin_count": 3},
        ])
        self.assertEqual((service.health(), service.events(),
                          list(service._store._connection.iterdump())), before)
        self.assertEqual(service.archive_capacity()["state"], "full")
        status, capabilities = self.request(path="/api/v1/capabilities", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(capabilities["data"]["features"]["averaged_sweep_change_detection"])
        for capability in ("averaged_sweep_reference", "sweep_context", "three_sweep_thresholds",
                           "candidate_frequency_rounding"):
            self.assertTrue(capabilities["data"]["features"][capability])

    def test_invalid_missing_and_query_requests_are_explicit(self):
        for payload in (_request(minimum_bins=True), _request(extra="no"),
                        _request(minimum_increase_db=float("nan"))):
            status, response = self.request(payload)
            self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        duplicated = (
            b'{"reference_ids":["sweep-0","sweep-1"],"target_id":"sweep-2",'
            b'"minimum_increase_db":6,"minimum_bins":2,"minimum_bins":2}'
        )
        status, response = self.request(raw=duplicated)
        self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        status, response = self.request(path="/api/v1/sessions/missing/sweep-average-change-detection")
        self.assertEqual((status, response["error"]["code"]), (404, "session_not_found"))
        status, response = self.request(_request(target_id="missing"))
        self.assertEqual((status, response["error"]["code"]), (404, "sweep_not_found"))
        status, response = self.request(path=self.path + "?minimum_bins=1")
        self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))


    def raw_headers(self, headers, body=b"", *, truncated=False):
        with socket.create_connection(self.server.server_address, timeout=5) as connection:
            connection.sendall((f"POST {self.path} HTTP/1.1\r\nHost: localhost\r\n" + headers + "\r\n").encode() + body)
            if truncated:
                connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            result = response.status, json.loads(response.read())
            self.assertEqual(connection.recv(1), b"")
            return result

    def test_strict_json_utf8_duplicate_keys_nonfinite_and_exact_shape(self):
        valid = _request()
        invalid = [b"", b"{", b"[]", b"null", b"true", b"{}", b"\xff", b"\xef\xbb\xbf{}",
                   b"[" * 1200 + b"0" + b"]" * 1200]
        for key, value in valid.items():
            invalid.append((json.dumps(valid)[:-1] + ', ' + json.dumps(key) + ': ' + json.dumps(value) + '}').encode())
            missing = dict(valid)
            del missing[key]
            invalid.append(json.dumps(missing).encode())
        for extra in ('"extra": {"x": 1, "x": 2}', '"extra": NaN', '"extra": Infinity',
                      '"extra": 1e999', r'"extra": "\ud800"'):
            invalid.append((json.dumps(valid)[:-1] + ', ' + extra + '}').encode())
        for key, values in (
            ("minimum_increase_db", (True, None, "6", [], {}, -1, 121, 10**1000,
                                     float("nan"), float("inf"), -float("inf"))),
            ("minimum_bins", (True, None, "2", [], {}, 2.0, 0, -1, 16_385, 10**1000)),
            ("target_id", (True, None, "", " ", "\ud800", "x" * 129, "sweep-0")),
            ("reference_ids", (True, None, [], ["a"], ["a", "a"], ["sweep-0", "sweep-2"],
                               ["sweep-0", "\ud800"], ["sweep-0", "x" * 129], list(map(str, range(17))))),
        ):
            invalid.extend(json.dumps(dict(valid, **{key: value})).encode() for value in values)
        service = self.server.session_service
        before = (service.health(), service.events(), service._store._connection.total_changes,
                  list(service._store._connection.iterdump()))
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                status, response = self.request(raw=raw)
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual((service.health(), service.events(), service._store._connection.total_changes,
                          list(service._store._connection.iterdump())), before)

    def test_query_bare_delimiter_and_get_rejected(self):
        for suffix in ("?", "?x=1", "?x=", "?&", "?minimum_bins=1"):
            with self.subTest(suffix=suffix):
                status, response = self.request(path=self.path + suffix)
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.request(method="GET")[0], 404)

    def test_missing_foreign_incompatible_and_overflow_statuses(self):
        for changes, expected, code in (
            ({"target_id": "missing"}, 404, "sweep_not_found"),
            ({"target_id": "sweep-5"}, 404, "sweep_not_found"),
            ({"reference_ids": ["sweep-0", "sweep-5"]}, 404, "sweep_not_found"),
            ({"reference_ids": ["sweep-0", "missing"]}, 404, "sweep_not_found"),
            ({"target_id": "sweep-3"}, 409, "average_reference_incompatible"),
            ({"reference_ids": ["sweep-0", "sweep-3"]}, 409, "average_reference_incompatible"),
            ({"reference_ids": ["sweep-6", "sweep-7"], "target_id": "sweep-8"},
             409, "average_reference_incompatible"),
        ):
            with self.subTest(changes=changes):
                status, response = self.request(_request(**changes))
                self.assertEqual((status, response["error"]["code"]), (expected, code))
        status, response = self.request(_request(target_id=" encoded/+ sweep "))
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["comparison"]["target"]["id"], " encoded/+ sweep ")

    def test_request_byte_limit_exact_boundary_and_leading_zero_length(self):
        for size in (str(MAX_AVERAGE_REQUEST_BYTES + 1), "9" * 5000):
            status, response = self.raw_headers(f"Content-Length: {size}\r\n")
            self.assertEqual((status, response["error"]["code"]), (413, "request_too_large"))
        raw = json.dumps(_request()).encode()
        self.assertEqual(self.request(raw=raw + b" " * (MAX_AVERAGE_REQUEST_BYTES - len(raw)))[0], 200)
        self.assertEqual(self.raw_headers(f"Content-Length: 000{len(raw)}\r\n", raw)[0], 200)
        status, response = self.request(raw=raw + b" " * (MAX_AVERAGE_REQUEST_BYTES + 1 - len(raw)))
        self.assertEqual((status, response["error"]["code"]), (413, "request_too_large"))

    def test_response_budget_excess_is_409_without_any_store_or_event_changes(self):
        service = self.server.session_service
        before = (service.health(), service.events(), service._store._connection.total_changes,
                  list(service._store._connection.iterdump()))
        status, response = self.request()
        self.assertEqual(status, 200)
        response_size = len(json_dumps(response).encode("utf-8"))
        with patch("radioinspector.sweep_average.MAX_CHANGE_DETECTION_RESPONSE_BYTES", response_size):
            self.assertEqual(self.request()[0], 200)
        with patch("radioinspector.sweep_average.MAX_CHANGE_DETECTION_RESPONSE_BYTES", response_size - 1):
            status, response = self.request()
        self.assertEqual((status, response["error"]["code"]), (409, "average_reference_incompatible"))
        self.assertEqual((service.health(), service.events(), service._store._connection.total_changes,
                          list(service._store._connection.iterdump())), before)

    def test_strict_framing_malformed_duplicate_lengths_transfer_encoding_and_truncation(self):
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
