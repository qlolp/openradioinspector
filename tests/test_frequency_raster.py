"""Portable frequency raster tables and read-only rounding of candidate frequencies."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from radioinspector.contracts import to_primitive
from radioinspector.frequency_raster import (
    MAX_FREQUENCY_HZ,
    MAX_RASTER_BYTES,
    MAX_RASTER_ENTRIES,
    MAX_ROUNDING_CANDIDATES,
    RASTER_FORMAT,
    calculate_frequency_rounding,
    parse_frequency_raster,
    round_frequency,
    validate_rounding_request,
)
from radioinspector.session_service import (
    CandidateNotFoundError, SessionNotFoundError, SessionService,
)
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_investigations import BASE, candidate_payload
from tests.test_sweep_comparison import create_session

ENTRY = {"name": "FM", "start_hz": 87_500_000, "stop_hz": 108_000_000, "grid_step_hz": 100_000}


MISSING = object()


def table(entries=MISSING, **changes):
    document = {
        "format": RASTER_FORMAT, "format_version": 1, "name": "Сетка",
        "frequency_unit": "Hz",
        "entries": [ENTRY] if entries is MISSING else entries,
    }
    document.update(changes)
    return document


def table_json(entries=MISSING, **changes):
    return json.dumps(table(entries, **changes), ensure_ascii=False)


def request_body(candidate_ids=("candidate-1",), table_json_value=MISSING, **changes):
    body = {
        "table_json": table_json() if table_json_value is MISSING else table_json_value,
        "candidate_ids": list(candidate_ids),
    }
    body.update(changes)
    return body


def make_candidate(service, session, candidate_id, frequency_hz):
    payload = candidate_payload(session.streams[0].id, candidate_id)
    payload["center_frequency_hz"] = frequency_hz
    return service.create_candidate(session.id, payload)


class RasterContractTests(unittest.TestCase):
    def test_request_exact_shape_id_bounds_utf8_and_uniqueness(self):
        invalid = [None, [], True, {}, dict(request_body(), extra=1), {"table_json": "[]"}]
        for field in request_body():
            body = request_body()
            del body[field]
            invalid.append(body)
        invalid += [{"table_json": table_json(), "candidate_ids": value}
                    for value in (None, [], True, 1, "x", [""], [" "], [1], [None],
                                  ["x" * 129], ["😀" * 129], ["\ud800"], ["a", "a"],
                                  ["a"] * (MAX_ROUNDING_CANDIDATES + 1))]
        invalid += [request_body(table_json_value=value)
                    for value in (None, 1, [], {}, b"x")]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises((ValueError, TypeError)):
                validate_rounding_request(body)
        self.assertEqual(validate_rounding_request(request_body()), (table_json(), ["candidate-1"]))
        self.assertEqual(validate_rounding_request(request_body((" exact/+ id ", "😀" * 128))),
                         (table_json(), [" exact/+ id ", "😀" * 128]))

    def test_table_fields_format_version_units_and_names(self):
        for changes in (
            {"extra": 1}, {"format": "x"}, {"format_version": 2}, {"format_version": True},
            {"format_version": 1.0}, {"frequency_unit": "kHz"}, {"frequency_unit": None},
            {"name": ""}, {"name": " \t\n"}, {"name": "\ud800"}, {"entries": None},
            {"entries": []}, {"entries": {}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_frequency_raster(json.dumps(table(**changes)))
        with self.assertRaises(ValueError):
            parse_frequency_raster("[]")
        with self.assertRaises(ValueError):
            parse_frequency_raster('{"format": "x", "format": "y", "format_version": 1,'
                                   ' "name": "a", "frequency_unit": "Hz", "entries": [' + json.dumps(ENTRY) + ']}')
        with self.assertRaises(ValueError):
            parse_frequency_raster("NaN")
        parsed = parse_frequency_raster(table_json())
        self.assertEqual(parsed["name"], "Сетка")
        self.assertEqual(parsed["entries"], [ENTRY])

    def test_entry_fields_ranges_steps_overlap_and_names(self):
        variants = [
            [{"name": "x", "start_hz": 1, "stop_hz": 2}],
            [{**ENTRY, "extra": 1}],
            [{**ENTRY, "start_hz": True}],
            [{**ENTRY, "start_hz": 1.0}],
            [{**ENTRY, "stop_hz": ENTRY["start_hz"]}],
            [{**ENTRY, "stop_hz": ENTRY["start_hz"] - 1}],
            [{**ENTRY, "stop_hz": MAX_FREQUENCY_HZ + 1}],
            [{**ENTRY, "grid_step_hz": 0}],
            [{**ENTRY, "grid_step_hz": -1}],
            [{**ENTRY, "grid_step_hz": ENTRY["stop_hz"] - ENTRY["start_hz"] + 1}],
            [{**ENTRY, "name": ""}],
            [{**ENTRY, "name": "a"}, {**ENTRY, "name": "a", "start_hz": 200_000_000, "stop_hz": 300_000_000}],
            [{**ENTRY, "name": "a"}, {**ENTRY, "name": "b"}],
            [{**ENTRY, "name": "b", "start_hz": 107_000_000, "stop_hz": 120_000_000, "grid_step_hz": 100_000},
             {**ENTRY, "name": "a"}],
        ]
        for entries in variants:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                parse_frequency_raster(json.dumps(table(entries)))
        many = [dict(ENTRY, name=f"e{i}", start_hz=1_000_000_000 + i * 1_000_000,
                     stop_hz=1_000_000_000 + i * 1_000_000 + 900_000, grid_step_hz=100_000)
                for i in range(MAX_RASTER_ENTRIES + 1)]
        with self.assertRaises(ValueError):
            parse_frequency_raster(json.dumps(table(many)))
        parse_frequency_raster(json.dumps(table([
            {"name": "touch", "start_hz": 87_400_000, "stop_hz": 87_500_000, "grid_step_hz": 100_000},
            {"name": "next", "start_hz": 87_500_001, "stop_hz": 108_000_000, "grid_step_hz": 100_000},
        ])))

    def test_table_size_limit_in_utf8_bytes(self):
        huge = json.dumps(table([dict(ENTRY, name="x" * 70_000)]))
        self.assertGreater(len(huge.encode()), MAX_RASTER_BYTES)
        with self.assertRaises(ValueError):
            parse_frequency_raster(huge)

    def test_rounding_nearest_ties_up_clamp_and_unmatched(self):
        parsed = parse_frequency_raster(table_json())
        cases = [
            (87_500_000, 87_500_000, 0),
            (87_550_000, 87_600_000, 50_000),   # tie → higher grid point
            (87_549_999, 87_500_000, -49_999),
            (101_234_567, 101_200_000, -34_567),
            (108_000_000, 108_000_000, 0),
            (107_999_999, 108_000_000, 1),
        ]
        for frequency, rounded, delta in cases:
            with self.subTest(frequency=frequency):
                result = round_frequency(frequency, parsed)
                self.assertEqual(result["rounded_frequency_hz"], rounded)
                self.assertEqual(result["delta_hz"], delta)
                self.assertTrue(result["matched"])
                self.assertEqual(result["entry_name"], "FM")
        for frequency in (0, 87_499_999, 108_000_001, MAX_FREQUENCY_HZ):
            with self.subTest(frequency=frequency):
                result = round_frequency(frequency, parsed)
                self.assertFalse(result["matched"])
                self.assertIsNone(result["rounded_frequency_hz"])
                self.assertIsNone(result["delta_hz"])
                self.assertIsNone(result["entry_name"])
        for bad in (True, False, 1.0, "1", None, -1, MAX_FREQUENCY_HZ + 1):
            with self.subTest(frequency=bad), self.assertRaises((ValueError, TypeError)):
                round_frequency(bad, parsed)

    def test_rounding_single_point_entry_clamps_to_its_only_grid_point(self):
        parsed = parse_frequency_raster(json.dumps(table([
            {"name": "wide", "start_hz": 0, "stop_hz": 25, "grid_step_hz": 10},
        ])))
        # Grid points 0, 10, 20; 25 would round to 30 which is outside → clamp to 20.
        self.assertEqual(round_frequency(25, parsed)["rounded_frequency_hz"], 20)
        self.assertEqual(round_frequency(25, parsed)["delta_hz"], -5)
        self.assertEqual(round_frequency(0, parsed)["rounded_frequency_hz"], 0)

    def test_calculation_echoes_sources_detached_and_counts(self):
        parsed = parse_frequency_raster(table_json())
        candidate_one = type("C", (), {"id": "candidate-1", "center_frequency_hz": 101_234_567})()
        candidate_two = type("C", (), {"id": "candidate-2", "center_frequency_hz": 50_000_000})()
        result = calculate_frequency_rounding("session", [candidate_one, candidate_two], parsed)
        self.assertEqual(set(result), {"session_id", "order", "table", "count",
                                       "matched_count", "unmatched_count", "items"})
        self.assertEqual((result["session_id"], result["order"], result["count"]), ("session", "request", 2))
        self.assertEqual((result["matched_count"], result["unmatched_count"]), (1, 1))
        self.assertEqual([item["candidate_id"] for item in result["items"]],
                         ["candidate-1", "candidate-2"])
        self.assertEqual(result["items"][0]["rounded_frequency_hz"], 101_200_000)
        self.assertIsNone(result["items"][1]["rounded_frequency_hz"])
        result["table"]["entries"][0]["name"] = "changed"
        result["items"][0]["frequency_hz"] = 1
        self.assertEqual(parsed["entries"][0]["name"], "FM")


class RoundingServiceMixin:
    def add_candidate(self, candidate_id, frequency_hz):
        return make_candidate(self.service, self.session, candidate_id, frequency_hz)

    def round_frequencies(self, candidate_ids=("candidate-1",), **changes):
        return self.service.frequency_rounding(self.session.id, request_body(candidate_ids, **changes))

    def test_rounds_archived_candidates_in_request_order_without_writes(self):
        self.add_candidate("candidate-2", 50_000_000)
        self.add_candidate("candidate-1", 101_234_567)
        result = self.round_frequencies(("candidate-1", "candidate-2"))
        self.assertEqual(result["order"], "request")
        self.assertEqual([item["candidate_id"] for item in result["items"]],
                         ["candidate-1", "candidate-2"])
        self.assertEqual(result["items"][0]["frequency_hz"], 101_234_567)
        self.assertEqual(result["items"][0]["rounded_frequency_hz"], 101_200_000)
        self.assertEqual(result["items"][0]["delta_hz"], -34_567)
        self.assertFalse(result["items"][1]["matched"])
        before = (self.service.health(), self.service.events(),
                  [to_primitive(item) for item in self.service.list_candidates(self.session.id)])
        self.round_frequencies(("candidate-1", "candidate-2"))
        after = (self.service.health(), self.service.events(),
                 [to_primitive(item) for item in self.service.list_candidates(self.session.id)])
        self.assertEqual(before, after)

    def test_unknown_candidate_foreign_session_and_missing_session(self):
        self.add_candidate("candidate-1", 101_234_567)
        with self.assertRaises(CandidateNotFoundError):
            self.round_frequencies(("candidate-404",))
        other = create_session(self.service)
        make_candidate(self.service, other, "foreign", 101_234_567)
        with self.assertRaises(CandidateNotFoundError):
            self.round_frequencies(("foreign",))
        with self.assertRaises(SessionNotFoundError):
            self.service.frequency_rounding("no-such-session", request_body())

    def test_bad_table_rejected_before_lookup(self):
        with self.assertRaises(ValueError):
            self.round_frequencies(table_json_value="{}")
        with self.assertRaises(ValueError):
            self.round_frequencies(table_json_value=json.dumps(table([dict(ENTRY, grid_step_hz=0)])))

    def test_capability(self):
        self.assertTrue(self.service.capabilities()["features"]["frequency_raster_rounding"])


class MemoryRoundingTests(RoundingServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=50)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class SQLiteRoundingTests(RoundingServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class RoundingHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(self.path))
        self.session = create_session(service)
        make_candidate(service, self.session, "candidate-1", 101_234_567)
        make_candidate(service, self.session, "candidate-2", 50_000_000)
        other = create_session(service)
        make_candidate(service, other, "foreign", 101_234_567)
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/frequency-rounding"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, body, *, path=None, raw=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", path or self.root,
                               body=raw if raw is not None else json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_rounds_over_http_and_reads_candidates_of_the_session_only(self):
        status, envelope = self.request(request_body(("candidate-1", "candidate-2")))
        self.assertEqual(status, 200)
        self.assertEqual(envelope["contract"], "openradioinspector-platform")
        data = envelope["data"]
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["items"][0]["rounded_frequency_hz"], 101_200_000)
        self.assertFalse(data["items"][1]["matched"])

    def test_error_statuses(self):
        cases = [
            (request_body(("candidate-404",)), 404, "candidate_not_found"),
            (request_body(("foreign",)), 404, "candidate_not_found"),
            (request_body(table_json="{}"), 400, "invalid_request"),
            ({"table_json": table_json()}, 400, "invalid_request"),
            (dict(request_body(), extra=1), 400, "invalid_request"),
        ]
        for body, status, error in cases:
            with self.subTest(body=body):
                code, envelope = self.request(body)
                self.assertEqual(code, status)
                self.assertEqual(envelope["error"]["code"], error)

    def test_query_parameters_rejected(self):
        code, envelope = self.request(request_body(), path=self.root + "?x=1")
        self.assertEqual(code, 400)
        self.assertEqual(envelope["error"]["code"], "invalid_request")

    def test_missing_session_is_404(self):
        code, envelope = self.request(request_body(),
                                      path="/api/v1/sessions/no-such-session/frequency-rounding")
        self.assertEqual(code, 404)
        self.assertEqual(envelope["error"]["code"], "session_not_found")

    def test_capability_advertised(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", "/api/v1/capabilities")
            response = connection.getresponse()
            envelope = json.loads(response.read())
        finally:
            connection.close()
        self.assertTrue(envelope["data"]["features"]["frequency_raster_rounding"])


if __name__ == "__main__":
    unittest.main()
