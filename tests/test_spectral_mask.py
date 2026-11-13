"""Synthetic-only spectral masks: strict artifacts, math, read-only storage, HTTP."""

from __future__ import annotations

import copy
import http.client
import json
import math
import queue
import random
import socket
import sys
import tempfile
import threading
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from radioinspector.contracts import Sweep
from radioinspector.session_service import SessionNotFoundError, SessionService, SweepNotFoundError
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.spectral_mask import (
    MASK_FORMAT, MAX_FREQUENCY_HZ, MAX_MASK_BYTES, MAX_MASK_REQUEST_BYTES,
    MaskIncompatibleError, calculate_sweep_mask, parse_spectral_mask,
)
from radioinspector.sweep_comparison import MAX_COMPARISON_POINTS
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session, measurement


def artifact(vertices=None, **changes):
    result = {
        "format": MASK_FORMAT, "format_version": 1, "name": "Mask",
        "frequency_unit": "Hz", "level_unit": "dBm", "interpolation": "linear_dbm",
        "vertices": [{"frequency_hz": f, "limit_dbm": v}
                     for f, v in (vertices or [(0, -80), (MAX_FREQUENCY_HZ, -80)])],
    }
    result.update(changes)
    return result


def document(vertices=None, **changes):
    return json.dumps(artifact(vertices, **changes), ensure_ascii=False)


class SpectralMaskArtifactTests(unittest.TestCase):
    def test_canonical_name_and_versioned_round_trip(self):
        result = parse_spectral_mask(document(name="  Cafe\u0301 🌈  "))
        self.assertEqual(result["name"], "Café 🌈")
        self.assertEqual(set(result), set(artifact()))
        self.assertEqual(parse_spectral_mask(json.dumps(result)), result)
        self.assertEqual(parse_spectral_mask(document(name="😀" * 80))["name"], "😀" * 80)
        self.assertEqual(result["vertices"][0]["frequency_hz"], 0)
        self.assertEqual(result["vertices"][-1]["frequency_hz"], MAX_FREQUENCY_HZ)

    def test_exact_fields_and_types(self):
        invalid = [None, [], 1, True, "mask"]
        for field in artifact():
            invalid.append({k: v for k, v in artifact().items() if k != field})
        for field, values in {
            "format": ["other", None, 1], "format_version": [0, 2, True, 1.0, "1", None],
            "name": ["", "  ", "x" * 81, "😀" * 81, None, 1, True],
            "frequency_unit": ["MHz", None], "level_unit": ["dB", "s_meter_count", None],
            "interpolation": ["linear_power", "step", None],
            "vertices": [None, {}, "", [], [{}], [None, None]],
        }.items():
            for value in values:
                invalid.append(dict(artifact(), **{field: value}))
        invalid.append(artifact(extra=1))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_spectral_mask(json.dumps(value))
        for value in (None, {}, [], b"{}", 2, True):
            with self.subTest(raw=value), self.assertRaises(ValueError):
                parse_spectral_mask(value)

    def test_reject_controls_before_trim_and_other_unicode_c_categories(self):
        for char in ("\n", "\t", "\x00", "\x7f", "\u200b", "\u202e", "\ue000", "\u0378", "\ud800"):
            with self.subTest(char=repr(char)), self.assertRaises(ValueError):
                parse_spectral_mask(json.dumps(artifact(name="mask" + char)))

    def test_vertex_limits_strict_order_integer_hz_and_finite_double(self):
        self.assertEqual(len(parse_spectral_mask(document([(i, -80) for i in range(128)]))["vertices"]), 128)
        invalid = [[(i, -80) for i in range(129)], [(0, -80)], [(0, -80), (0, -70)],
                   [(2, -80), (1, -70)], [(-1, -80), (1, -70)],
                   [(0, -80), (MAX_FREQUENCY_HZ + 1, -70)]]
        for value in (True, False, 0.0, "0", None):
            invalid.append([(value, -80), (10, -70)])
        for value in (True, False, None, "-80", float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            invalid.append([(0, value), (10, -70)])
        for vertices in invalid:
            with self.subTest(vertices=vertices), self.assertRaises(ValueError):
                parse_spectral_mask(document(vertices))
        for vertex in ({"frequency_hz": 0}, {"limit_dbm": -80},
                       {"frequency_hz": 0, "limit_dbm": -80, "extra": 1}):
            with self.subTest(vertex=vertex), self.assertRaises(ValueError):
                parse_spectral_mask(json.dumps(dict(artifact(), vertices=[vertex, {"frequency_hz": 10, "limit_dbm": -70}])))

    def test_strict_json_duplicates_and_encoding(self):
        valid = document()
        invalid = ["", "{", valid + "{}", "\ufeff" + valid,
                   valid.replace('"name": "Mask"', '"name": "Mask", "name": "other"'),
                   valid.replace('"frequency_hz": 0', '"frequency_hz": 0, "frequency_hz": 1'),
                   valid.replace('"limit_dbm": -80', '"limit_dbm": NaN', 1),
                   valid.replace('"limit_dbm": -80', '"limit_dbm": Infinity', 1),
                   valid.replace('"limit_dbm": -80', '"limit_dbm": 1e999', 1),
                   valid.replace('"limit_dbm": -80', '"limit_dbm": ' + "9" * 5000, 1),
                   valid.replace('"name": "Mask"', '"name": "\\ud800"'),
                   valid.replace('"name": "Mask"', '"name": "\ud800"'),
                   "[" * 1200 + "0" + "]" * 1200]
        for raw in invalid:
            with self.subTest(raw=raw[:100]), self.assertRaises(ValueError):
                parse_spectral_mask(raw)

    def test_byte_limit_is_utf8_not_character_count(self):
        raw = document(name="🌈")
        boundary = raw + " " * (MAX_MASK_BYTES - len(raw.encode("utf-8")))
        self.assertEqual(parse_spectral_mask(boundary)["name"], "🌈")
        with self.assertRaises(ValueError):
            parse_spectral_mask(boundary + " ")


class SpectralMaskMathTests(unittest.TestCase):
    def calculate(self, frequencies, levels, vertices=None, **changes):
        sweep = Sweep.from_dict(measurement(frequencies_hz=frequencies, levels=levels, **changes))
        return calculate_sweep_mask("session", sweep, mask_json=document(vertices))

    def test_horizontal_full_shape_provenance_and_no_mutation(self):
        sweep = Sweep.from_dict(measurement(settings={"rbw_hz": 100, "x": [1]}))
        before = copy.deepcopy(sweep)
        result = calculate_sweep_mask("session", sweep, mask_json=document(name=" Cafe\u0301 "))
        self.assertEqual(set(result), {"session_id", "stream_id", "sweep", "mask", "frequencies_hz",
                                      "threshold_levels", "level_unit", "excess_levels", "delta_unit",
                                      "comparison", "regions", "points", "exceedance_points", "region_count"})
        self.assertEqual(result["mask"]["name"], "Café")
        self.assertEqual(result["sweep"], {"id": sweep.id, "interval": measurement()["interval"],
                                         "levels": list(sweep.levels), "level_unit": "dBm", "settings": sweep.settings})
        self.assertEqual(result["threshold_levels"], [-80] * 4)
        self.assertEqual(result["excess_levels"], [0, 10, 20, 30])
        self.assertEqual(result["comparison"], "strictly_greater")
        self.assertEqual(result["delta_unit"], "dB")
        self.assertEqual(result["regions"], [{"start_index": 1, "end_index": 3,
                         "start_frequency_hz": 200, "end_frequency_hz": 400, "peak_index": 3,
                         "peak_frequency_hz": 400, "peak_excess_db": 30, "points": 3}])
        result["sweep"]["settings"]["x"].append(2)
        result["sweep"]["levels"][0] = 100
        result["frequencies_hz"][0] = 999
        result["sweep"]["interval"]["started_at"] = "changed"
        self.assertEqual(sweep, before)

    def test_angled_multisegment_duplicate_irregular_bins_and_exact_vertices(self):
        result = self.calculate([0, 2, 2, 5, 10, 11, 15, 20], [-50] * 8,
                                [(0, -100), (10, -60), (20, -80)])
        self.assertEqual(result["threshold_levels"], [-100, -92, -92, -80, -60, -62, -70, -80])
        self.assertEqual(result["excess_levels"], [50, 42, 42, 30, 10, 12, 20, 30])
        self.assertEqual(result["frequencies_hz"], [0, 2, 2, 5, 10, 11, 15, 20])
        self.assertEqual(result["regions"][0]["points"], 8)
        self.assertEqual(self.calculate([0, 5, 10], [-100, -80, -60], [(0, -100), (10, -60)])["regions"], [])

    def test_independent_fraction_oracle_for_piecewise_linear_dbm(self):
        randomizer = random.Random(42)
        vertices = [(i * 100, randomizer.randint(-160, 40)) for i in range(20)]
        frequencies = sorted(randomizer.choices(range(1901), k=500))
        result = self.calculate(frequencies, [-50] * len(frequencies), vertices)
        for f, actual in zip(frequencies, result["threshold_levels"]):
            left, right = next((a, b) for a, b in zip(vertices, vertices[1:]) if a[0] <= f <= b[0])
            exact = Fraction(left[1]) + Fraction((right[1] - left[1]) * (f - left[0]), right[0] - left[0])
            self.assertTrue(math.isclose(actual, float(exact), rel_tol=1e-13, abs_tol=1e-12))

    def test_maximal_index_runs_inclusive_and_first_peak_ties(self):
        frequencies = [0, 0, 10, 100, 100, 101, 500, 501]
        result = self.calculate(frequencies, [-79, -78, -78, -80, -81, -75, -80, -70])
        self.assertEqual([(r["start_index"], r["end_index"], r["peak_index"], r["points"])
                          for r in result["regions"]], [(0, 2, 1, 3), (5, 5, 5, 1), (7, 7, 7, 1)])
        self.assertEqual(result["regions"][0]["start_frequency_hz"], 0)
        self.assertEqual(result["regions"][0]["end_frequency_hz"], 10)
        self.assertEqual(result["regions"][0]["peak_frequency_hz"], 0)
        self.assertEqual(result["exceedance_points"], 5)
        self.assertEqual(result["region_count"], 3)

    def test_single_bin_and_maximum_bins_no_truncation(self):
        result = self.calculate([2], [-79])
        self.assertEqual(result["points"], 1)
        self.assertEqual(result["regions"][0]["points"], 1)
        count = MAX_COMPARISON_POINTS
        result = self.calculate(list(range(count)), [-90, -50] * (count // 2))
        self.assertEqual(result["points"], count)
        self.assertEqual(result["exceedance_points"], count // 2)
        self.assertEqual(result["region_count"], count // 2)
        self.assertEqual(result["regions"][-1]["end_index"], count - 1)
        with self.assertRaises(MaskIncompatibleError):
            self.calculate(list(range(count + 1)), [-90] * (count + 1))

    def test_source_gate_and_coverage_no_extrapolation(self):
        for changes in ({"session_id": "other"}, {"level_unit": "s_meter_count"}, {"level_unit": "dB"},
                        {"settings": {}}, {"settings": {"sequence": 1}}, {"settings": {"rbw_hz": None}},
                        {"settings": {"x": [None]}}, {"settings": {"x": float("inf")}},
                        {"settings": {"x": object()}}, {"settings": {"x": 1, "sequence": float("nan")}}):
            with self.subTest(changes=changes), self.assertRaises(MaskIncompatibleError):
                self.calculate([1, 2], [-90, -90], **changes)
        for frequencies in ([0], [11], [0, 11]):
            with self.subTest(frequencies=frequencies), self.assertRaises(MaskIncompatibleError):
                self.calculate(frequencies, [-90] * len(frequencies), [(1, -80), (10, -80)])
        with self.assertRaises(MaskIncompatibleError):
            self.calculate([MAX_FREQUENCY_HZ + 1], [-90])
        self.assertEqual(self.calculate([MAX_FREQUENCY_HZ], [-90])["threshold_levels"], [-80])
        self.calculate([1], [-90], settings={"rbw_hz": 100, "sequence": None})

    def test_extreme_finite_interpolation_and_rejected_derived_overflow(self):
        largest, smallest = sys.float_info.max, math.ulp(0.0)
        for value in (largest, -largest, smallest, -smallest):
            result = self.calculate([0, 1, 2, 3, 4], [value] * 5, [(0, value), (4, value)])
            self.assertEqual(result["threshold_levels"], [value] * 5)
            self.assertEqual(result["excess_levels"], [0] * 5)
            json.dumps(result, allow_nan=False)
        result = self.calculate([0, 1, 2, 3, 4], [0] * 5, [(0, -largest), (4, largest)])
        for actual, expected in zip(result["threshold_levels"], [-largest, -largest / 2, 0, largest / 2, largest]):
            self.assertTrue(math.isclose(actual, expected, rel_tol=3e-16))
        for vertices in ([(0, largest), (4, largest / 2)], [(0, -largest), (4, -largest / 2)]):
            result = self.calculate([0, 1, 2, 3, 4], [0] * 5, vertices)
            self.assertTrue(all(math.isfinite(v) for v in result["threshold_levels"]))
        with self.assertRaises(MaskIncompatibleError):
            self.calculate([1], [largest], [(0, -largest), (2, -largest)])
        result = self.calculate([MAX_FREQUENCY_HZ - 1], [0], [(0, 0), (MAX_FREQUENCY_HZ, 100)])
        self.assertLess(result["threshold_levels"][0], 100)


class MaskServiceMixin:
    def add(self, number=0, **changes):
        return self.service.ingest_sweep(measurement(self.session.id, self.stream_id, number, **changes))

    def calculate(self, sweep_id="sweep-0", **changes):
        return self.service.sweep_mask_analysis(self.session.id, {"sweep_id": sweep_id, "mask_json": document(**changes)})

    def snapshot(self):
        return copy.deepcopy((self.service.health(), self.service.events(), self.service.list_sessions(),
                              self.service.list_sweeps(self.session.id), self.service.archive_capacity(),
                              self.service.list_sweep_references(self.session.id),
                              self.service.list_candidates(self.session.id), self.service.list_investigations(self.session.id)))

    def test_outside_recent_window(self):
        for start, end in ((0, 100), (100, 112)):
            self.service.ingest_sweeps([measurement(self.session.id, self.stream_id, i) for i in range(start, end)])
        self.assertNotIn("sweep-0", [s.id for s in self.service.list_sweeps(self.session.id)])
        self.assertEqual(self.calculate()["sweep"]["id"], "sweep-0")

    def test_no_state_source_events_cursor_capacity_catalog_or_candidates_mutation(self):
        original = self.add(settings={"rbw_hz": 100, "extension": {"a": [1]}})
        self.add(1, settings={})
        self.add(2, levels=[sys.float_info.max] * 4)
        before = self.snapshot()
        subscriber_id, subscriber = self.service.subscribe(after=before[0]["last_event_id"])
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        result = self.calculate()
        result["sweep"]["settings"]["extension"]["a"].append(2)
        result["sweep"]["levels"][0] = 100
        result["frequencies_hz"][0] = 999
        result["mask"]["vertices"][0]["limit_dbm"] = 100
        with self.assertRaises(MaskIncompatibleError):
            self.calculate("sweep-1")
        with self.assertRaises(MaskIncompatibleError):
            self.calculate("sweep-2", vertices=[(0, -sys.float_info.max), (1000, -sys.float_info.max)])
        with self.assertRaises(MaskIncompatibleError):
            self.calculate(vertices=[(0, -80), (200, -80)])
        with self.assertRaises(ValueError):
            self.calculate(name="")
        with self.assertRaises(SweepNotFoundError):
            self.calculate("missing")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calculate()["sweep"]["levels"], list(original.levels))
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        self.add(3)
        self.assertEqual(self.service.health()["last_event_id"], before[0]["last_event_id"] + 1)

    def test_exact_missing_and_foreign_ids(self):
        self.add(id=" encoded/+ sweep ")
        self.assertEqual(self.calculate(" encoded/+ sweep ")["sweep"]["id"], " encoded/+ sweep ")
        second = create_session(self.service)
        self.service.ingest_sweep(measurement(second.id, second.streams[0].id, 1))
        for sweep_id in ("missing", "sweep-1", "encoded/+ sweep"):
            with self.subTest(sweep_id=sweep_id), self.assertRaises(SweepNotFoundError):
                self.calculate(sweep_id)
        with self.assertRaises(SessionNotFoundError):
            self.service.sweep_mask_analysis("missing", {"sweep_id": "sweep-0", "mask_json": document()})

    def test_strict_body_and_capability(self):
        self.add()
        valid = {"sweep_id": "sweep-0", "mask_json": document()}
        invalid = [None, [], True, {}, dict(valid, extra=1), {"sweep_id": "sweep-0"}, {"mask_json": document()}]
        invalid += [dict(valid, sweep_id=value) for value in (None, "", "  ", 10, True)]
        invalid += [dict(valid, mask_json=value) for value in (None, {}, 10, True, b"{}")]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.sweep_mask_analysis(self.session.id, payload)
        self.assertTrue(self.service.capabilities()["features"]["spectral_mask_analysis"])


class MemoryMaskTests(MaskServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteMemoryMaskTests(MaskServiceMixin, unittest.TestCase):
    def setUp(self):
        self.store = SQLiteSessionStore(":memory:")
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id


class SQLiteMaskTests(MaskServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(lambda: self.service.close())
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id

    def test_primary_key_reads_no_scan_write_or_schema_change(self):
        self.add()
        self.add(1, settings={})
        connection = self.store._connection
        before = connection.total_changes
        schema = list(connection.execute("SELECT * FROM sqlite_master"))
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            with patch.object(self.store, "list_sweeps", side_effect=AssertionError("no archive scan")):
                self.calculate()
                with self.assertRaises(MaskIncompatibleError):
                    self.calculate("sweep-1")
                with self.assertRaises(ValueError):
                    self.calculate(name="")
        finally:
            connection.set_trace_callback(None)
        self.assertEqual(connection.total_changes, before)
        self.assertEqual(list(connection.execute("SELECT * FROM sqlite_master")), schema)
        self.assertEqual(len(statements), 3)
        self.assertTrue(all(s.startswith("SELECT * FROM sweeps WHERE id = ") for s in statements))

    def test_restart_full_archive_preserves_exact_old_id_analysis(self):
        self.test_outside_recent_window()
        expected = self.calculate()
        size = self.service.archive_capacity()["database_bytes"]
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=size)
        self.service = SessionService(store=self.store, history_limit=2)
        self.assertEqual(self.service.archive_capacity()["state"], "full")
        self.assertEqual(self.calculate(), expected)


class MaskHTTPTests(unittest.TestCase):
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
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/sweep-mask-analysis"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, payload=None, *, raw=None, path=None, method="POST"):
        if raw is None:
            raw = json.dumps(payload if payload is not None else {"sweep_id": "sweep-0", "mask_json": document()}).encode()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path or self.root, body=raw, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw_headers(self, headers, body=b""):
        with socket.create_connection(self.server.server_address, timeout=5) as connection:
            connection.sendall((f"POST {self.root} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n" + headers + "\r\n").encode() + body)
            response = http.client.HTTPResponse(connection)
            response.begin()
            result = response.status, json.loads(response.read())
            # Every framing refusal must close, with no unread-body reuse.
            self.assertEqual(connection.recv(1), b"")
            return result

    def test_success_at_capacity_envelope_capability_and_encoded_id(self):
        service = self.server.session_service
        before = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity())
        status, envelope = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(envelope["contract_version"], 1)
        self.assertEqual(envelope["data"]["excess_levels"], [0, 10, 20, 30])
        self.assertEqual(envelope["data"]["mask"], parse_spectral_mask(document()))
        after = (service.health(), service.events(), service.list_sweeps(self.session.id), service.archive_capacity())
        self.assertEqual(before, after)
        self.assertEqual(after[-1]["state"], "full")
        status, capability = self.request(path="/api/v1/capabilities", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(capability["data"]["features"]["spectral_mask_analysis"])
        status, response = self.request({"sweep_id": " encoded/+ sweep ", "mask_json": document()})
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["sweep"]["id"], " encoded/+ sweep ")

    def test_strict_body_duplicate_fields_and_malformed_utf8(self):
        valid = {"sweep_id": "sweep-0", "mask_json": document()}
        invalid = [b"", b"{", b"[]", b"null", b"true", b"{}", b"\xff", b"\xef\xbb\xbf{}",
                   json.dumps(dict(valid, extra=1)).encode(),
                   json.dumps({"sweep_id": "sweep-0"}).encode(),
                   json.dumps({"mask_json": document()}).encode(),
                   (json.dumps(valid)[:-1] + ', "sweep_id": "sweep-0"}').encode(),
                   (json.dumps(valid)[:-1] + ', "mask_json": "{}"}').encode(),
                   (json.dumps(valid)[:-1] + ', "extra": {"x": 1, "x": 2}}').encode(),
                   (json.dumps(valid)[:-1] + ', "extra": NaN}').encode(),
                   (json.dumps(valid)[:-1] + ', "extra": "\\ud800"}').encode(),
                   b"[" * 1200 + b"0" + b"]" * 1200]
        for value in (None, True, 10, [], {}):
            invalid.append(json.dumps(dict(valid, mask_json=value)).encode())
        for value in (None, True, 10, "", "  ", "\ud800"):
            invalid.append(json.dumps(dict(valid, sweep_id=value)).encode())
        before = self.server.session_service.health()
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                status, response = self.request(raw=raw)
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.server.session_service.health(), before)

    def test_artifact_raw_text_remains_strict_at_http_boundary(self):
        valid = document()
        invalid = [valid.replace('"name": "Mask"', '"name": "Mask", "name": "other"'),
                   valid.replace('"frequency_hz": 0', '"frequency_hz": 0, "frequency_hz": 1'),
                   valid.replace('"limit_dbm": -80', '"limit_dbm": 1e999', 1),
                   valid.replace('"name": "Mask"', '"name": "\\ud800"'),
                   "[" * 1200 + "0" + "]" * 1200,
                   valid + " " * MAX_MASK_BYTES]
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                status, response = self.request({"sweep_id": "sweep-0", "mask_json": raw})
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))

    def test_no_queries_and_get_does_not_mutate(self):
        service = self.server.session_service
        before = (service.health(), service.events(), service.archive_capacity(), service.list_sweep_references(self.session.id))
        for suffix in ("?", "?x=1", "?x=", "?&", "?sweep_id=sweep-0"):
            status, response = self.request(path=self.root + suffix)
            self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.request(method="GET", raw=b"")[0], 404)
        self.assertEqual(before, (service.health(), service.events(), service.archive_capacity(), service.list_sweep_references(self.session.id)))

    def test_incompatible_missing_and_foreign_no_disclosure(self):
        for sweep_id, expected, code in (("missing", 404, "sweep_not_found"), ("sweep-3", 404, "sweep_not_found"),
                                        ("sweep-1", 409, "mask_incompatible"), ("sweep-4", 409, "mask_incompatible")):
            with self.subTest(sweep_id=sweep_id):
                status, response = self.request({"sweep_id": sweep_id,
                    "mask_json": document([(0, -sys.float_info.max), (1000, -sys.float_info.max)])})
                self.assertEqual((status, response["error"]["code"]), (expected, code))
        status, response = self.request(path="/api/v1/sessions/missing/sweep-mask-analysis")
        self.assertEqual((status, response["error"]["code"]), (404, "session_not_found"))
        status, response = self.request({"sweep_id": "sweep-0", "mask_json": document([(0, -80), (200, -80)])})
        self.assertEqual((status, response["error"]["code"]), (409, "mask_incompatible"))

    def test_request_size_bound_before_read_and_exact_boundary_accepted(self):
        for size in (str(MAX_MASK_REQUEST_BYTES + 1), "9" * 5000):
            status, response = self.raw_headers(f"Content-Length: {size}\r\n")
            self.assertEqual((status, response["error"]["code"]), (413, "request_too_large"))
        raw = json.dumps({"sweep_id": "sweep-0", "mask_json": document()}).encode()
        self.assertEqual(self.request(raw=raw + b" " * (MAX_MASK_REQUEST_BYTES - len(raw)))[0], 200)

    def test_malformed_duplicate_length_and_transfer_encoding(self):
        invalid = ["", "Content-Length: -1\r\n", "Content-Length: +1\r\n", "Content-Length: 1.0\r\n",
                   "Content-Length: 0, 0\r\n", "Content-Length: 0\r\nContent-Length: 0\r\n",
                   "Content-Length: 0\r\nTransfer-Encoding: chunked\r\n",
                   "Content-Length: 0\r\nTransfer-Encoding: identity\r\n",
                   "Content-Length: \r\n", "Transfer-Encoding: chunked\r\n"]
        for headers in invalid:
            with self.subTest(headers=headers):
                status, response = self.raw_headers(headers)
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))


if __name__ == "__main__":
    unittest.main()
