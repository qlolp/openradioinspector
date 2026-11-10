"""Synthetic-only integer grids, exact source provenance, read-only storage and HTTP."""

from __future__ import annotations

import copy
import http.client
import json
import queue
import random
import socket
import tempfile
import threading
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from radioinspector.contracts import Candidate, CandidateState, to_primitive
from radioinspector.frequency_rounding import (
    MAX_FREQUENCY_HZ, MAX_PROFILE_BYTES, MAX_ROUNDING_REQUEST_BYTES, ROUNDING_FORMAT,
    FrequencyRoundingIncompatibleError, calculate_candidate_frequency_rounding,
    parse_frequency_rounding_profile, validate_rounding_request,
)
from radioinspector.session_service import CandidateNotFoundError, SessionNotFoundError, SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_device_workers import simulated_session_payload
from tests.test_investigations import candidate_payload


def rule(start=0, end=MAX_FREQUENCY_HZ, step=10, origin=0, tolerance=MAX_FREQUENCY_HZ):
    return {"start_frequency_hz": start, "end_frequency_hz": end, "step_hz": step,
            "origin_frequency_hz": origin, "max_offset_hz": tolerance}


def artifact(rules=None, **changes):
    result = {"format": ROUNDING_FORMAT, "format_version": 1, "name": "Grid",
              "frequency_unit": "Hz", "method": "nearest_grid", "tie_break": "lower",
              "rules": [rule()] if rules is None else rules}
    result.update(changes)
    return result


def document(rules=None, **changes):
    return json.dumps(artifact(rules, **changes), ensure_ascii=False)


def request_body(candidate_id="candidate-1", profile_json=None):
    return {"candidate_id": candidate_id, "profile_json": document() if profile_json is None else profile_json}


def candidate(frequency=102_500_003, **changes):
    payload = candidate_payload("stream")
    payload.update(session_id="session", center_frequency_hz=frequency, **changes)
    return Candidate.from_dict(payload)


class FrequencyRoundingProfileTests(unittest.TestCase):
    def test_canonical_name_versioned_round_trip_and_limits(self):
        parsed = parse_frequency_rounding_profile(document(name="  Cafe\u0301 🌈  "))
        self.assertEqual(parsed["name"], "Café 🌈")
        self.assertEqual(set(parsed), set(artifact()))
        self.assertEqual(parsed["rules"], [rule()])
        self.assertEqual(parse_frequency_rounding_profile(json.dumps(parsed)), parsed)
        self.assertEqual(parse_frequency_rounding_profile(document(name="😀" * 80))["name"], "😀" * 80)
        rules = [rule(i, i, 1) for i in range(128)]
        self.assertEqual(parse_frequency_rounding_profile(document(rules))["rules"], rules)
        # Origins and steps can reach the safe maximum and need not lie inside the band.
        for item in (rule(0, 0, MAX_FREQUENCY_HZ, MAX_FREQUENCY_HZ, 0),
                     rule(MAX_FREQUENCY_HZ, MAX_FREQUENCY_HZ, MAX_FREQUENCY_HZ, 0, 0)):
            self.assertEqual(parse_frequency_rounding_profile(document([item]))["rules"], [item])

    def test_shared_example_artifact_is_valid_and_lossless(self):
        path = Path(__file__).resolve().parents[1] / "docs/examples/frequency-rounding-profile.json"
        raw = path.read_text(encoding="utf-8")
        self.assertEqual(parse_frequency_rounding_profile(raw), json.loads(raw))

    def test_exact_profile_fields_and_types(self):
        invalid = [None, [], True, 1, "grid", artifact(extra=1)]
        for field in artifact():
            invalid.append({k: v for k, v in artifact().items() if k != field})
        for field, values in {
            "format": [None, 1, "other"], "format_version": [None, 0, 2, True, 1.0, "1"],
            "name": [None, 1, True, "", "  ", "x" * 81, "😀" * 81],
            "frequency_unit": [None, "MHz"], "method": [None, "floor", "nearest"],
            "tie_break": [None, "upper", "even"],
            "rules": [None, {}, "", [], [None], [rule(i, i, 1) for i in range(129)]],
        }.items():
            invalid.extend(dict(artifact(), **{field: value}) for value in values)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_frequency_rounding_profile(json.dumps(value))
        for value in (None, {}, [], b"{}", True, 1):
            with self.subTest(raw=value), self.assertRaises(ValueError):
                parse_frequency_rounding_profile(value)

    def test_rule_types_fields_ranges_order_overlap_and_grid_existence(self):
        invalid = [[{}], [None], [dict(rule(), extra=1)],
                   [rule(10, 9)], [rule(0, 1, 0)], [rule(1, 2, 10)],
                   [rule(10, 20), rule(0, 9)], [rule(0, 10), rule(10, 20)],
                   [rule(0, 20), rule(10, 30)], [rule(0, 20), rule(10, 10)]]
        for field in rule():
            invalid.append([{k: v for k, v in rule().items() if k != field}])
            for value in (-1, MAX_FREQUENCY_HZ + 1, True, False, 1.0, "1", None, [], {}, 10**1000):
                invalid.append([dict(rule(), **{field: value})])
        for rules in invalid:
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                parse_frequency_rounding_profile(document(rules))
        self.assertEqual(len(parse_frequency_rounding_profile(document([rule(0, 10), rule(11, 20)]))["rules"]), 2)

    def test_strict_json_and_unicode(self):
        raw = document()
        invalid = ["", "{", raw + "{}", "\ufeff" + raw,
                   raw.replace('"name": "Grid"', '"name": "Grid", "name": "Other"'),
                   raw.replace('"step_hz": 10', '"step_hz": 10, "step_hz": 20'),
                   raw.replace('"step_hz": 10', '"step_hz": NaN'),
                   raw.replace('"step_hz": 10', '"step_hz": Infinity'),
                   raw.replace('"step_hz": 10', '"step_hz": -Infinity'),
                   raw.replace('"step_hz": 10', '"step_hz": 1e999'),
                   raw.replace('"step_hz": 10', '"step_hz": ' + "9" * 5000),
                   raw.replace('"name": "Grid"', '"name": "\\ud800"'),
                   raw.replace('"name": "Grid"', '"name": "\ud800"'),
                   "[" * 1200 + "0" + "]" * 1200]
        for value in invalid:
            with self.subTest(raw=value[:100]), self.assertRaises(ValueError):
                parse_frequency_rounding_profile(value)
        for char in ("\n", "\t", "\x00", "\x7f", "\u200b", "\u202e", "\ue000", "\u0378", "\ud800"):
            with self.subTest(char=repr(char)), self.assertRaises(ValueError):
                parse_frequency_rounding_profile(json.dumps(artifact(name="Grid" + char)))

    def test_exact_utf8_byte_boundary(self):
        raw = document(name="🌈")
        boundary = raw + " " * (MAX_PROFILE_BYTES - len(raw.encode("utf-8")))
        self.assertEqual(parse_frequency_rounding_profile(boundary)["name"], "🌈")
        with self.assertRaises(ValueError):
            parse_frequency_rounding_profile(boundary + " ")

    def test_request_shape_and_exact_id_utf8_codepoints(self):
        valid = request_body()
        invalid = [None, [], True, {}, dict(valid, extra=1), {"candidate_id": "x"}, {"profile_json": document()}]
        invalid += [dict(valid, candidate_id=value) for value in
                    (None, True, 1, {}, [], "", " \t\n", "x" * 129, "😀" * 129, "\ud800")]
        invalid += [dict(valid, profile_json=value) for value in (None, True, 1, {}, [], b"{}")]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_rounding_request(payload)
        for identifier in (" exact/+ candidate ", "😀" * 128, "é", "e\u0301", "\x00"):
            self.assertEqual(validate_rounding_request(request_body(identifier)), (identifier, document()))


class FrequencyRoundingMathTests(unittest.TestCase):
    def calculate(self, frequency, rules=None, **changes):
        return calculate_candidate_frequency_rounding("session", candidate(frequency, **changes),
                                                      profile_json=document(rules))

    def assert_result(self, frequency, expected, rules=None):
        self.assertEqual(self.calculate(frequency, rules)["result"], dict(zip(
            ("status", "rule_index", "nearest_frequency_hz", "offset_hz", "rounded_frequency_hz"), expected)))

    def test_all_four_statuses_tie_lower_signed_offsets_and_inclusive_tolerance(self):
        for frequency, expected in ((10, ("unchanged", 0, 10, 0, 10)),
                                    (14, ("rounded", 0, 10, -4, 10)),
                                    (15, ("rounded", 0, 10, -5, 10)),
                                    (16, ("rounded", 0, 20, 4, 20))):
            self.assert_result(frequency, expected)
        self.assert_result(15, ("rounded", 0, 10, -5, 10), [rule(tolerance=5)])
        self.assert_result(15, ("outside_tolerance", 0, 10, -5, None), [rule(tolerance=4)])
        self.assert_result(16, ("outside_tolerance", 0, 20, 4, None), [rule(tolerance=3)])
        self.assert_result(10, ("unchanged", 0, 10, 0, 10), [rule(tolerance=0)])
        self.assert_result(11, ("outside_tolerance", 0, 10, -1, None), [rule(tolerance=0)])
        self.assert_result(31, ("outside_ranges", None, None, None, None), [rule(10, 30)])

    def test_clamps_to_in_range_grid_not_band_edges_onepoint_and_negative_k(self):
        rules = [rule(13, 27, 10, 100)]  # k=-8 is the only in-range grid point.
        for frequency in (13, 14, 19, 20, 26, 27):
            expected = "unchanged" if frequency == 20 else "rounded"
            self.assert_result(frequency, (expected, 0, 20, 20-frequency, 20), rules)
        self.assert_result(13, ("outside_tolerance", 0, 20, 7, None), [rule(13, 27, 10, 100, 6)])
        for origin in (0, 10, 100, 1000):
            self.assert_result(25, ("rounded", 0, 20, -5, 20), [rule(11, 39, 10, origin)])
        self.assert_result(22, ("rounded", 0, 23, 1, 23), [rule(11, 39, 10, 3)])
        self.assert_result(10, ("unchanged", 0, 10, 0, 10), [rule(10, 10, 37, 84)])

    def test_multiple_inclusive_bands_no_gap_extrapolation_and_zero_grid(self):
        rules = [rule(0, 10, 10), rule(11, 20, 5), rule(30, 40, 5)]
        self.assert_result(1, ("rounded", 0, 0, -1, 0), rules)
        self.assert_result(10, ("unchanged", 0, 10, 0, 10), rules)
        self.assert_result(11, ("rounded", 1, 15, 4, 15), rules)
        self.assert_result(20, ("unchanged", 1, 20, 0, 20), rules)
        self.assert_result(30, ("unchanged", 2, 30, 0, 30), rules)
        self.assert_result(40, ("unchanged", 2, 40, 0, 40), rules)
        for frequency in (21, 25, 29, 41):
            self.assert_result(frequency, ("outside_ranges", None, None, None, None), rules)

    def test_exact_safe_integer_max_no_float_rounding_or_overflow(self):
        maximum = MAX_FREQUENCY_HZ
        self.assert_result(maximum, ("rounded", 0, maximum - 1, -1, maximum - 1), [rule(step=2)])
        self.assert_result(maximum - 1, ("rounded", 0, maximum - 2, -1, maximum - 2), [rule(step=2, origin=1)])
        self.assert_result(maximum - 1, ("rounded", 0, maximum, 1, maximum),
                           [rule(1, maximum, maximum, maximum)])
        self.assert_result(1, ("rounded", 0, maximum, maximum-1, maximum),
                           [rule(1, maximum, maximum, maximum)])
        self.assert_result(maximum, ("unchanged", 0, maximum, 0, maximum), [rule(step=maximum)])
        # Long spans and intermediate values above the safe maximum are integer-only.
        self.assert_result(maximum - 1, ("rounded", 0, maximum, 1, maximum), [rule(step=maximum)])

    def test_independent_enumeration_oracle_over_random_bands_and_extremes(self):
        randomizer = random.Random(178)
        checked = 0
        for _ in range(400):
            start = randomizer.choice((0, 1, 100, MAX_FREQUENCY_HZ - 100)) + randomizer.randrange(10)
            end = min(MAX_FREQUENCY_HZ, start + randomizer.randrange(1, 50))
            step = randomizer.randrange(1, 61)
            origin = randomizer.choice((0, MAX_FREQUENCY_HZ, randomizer.randrange(1000)))
            tolerance = randomizer.randrange(20)
            grid = [point for point in range(start, end + 1) if (point - origin) % step == 0]
            if not grid:
                continue
            item = rule(start, end, step, origin, tolerance)
            for frequency in range(max(1, start - 1), min(MAX_FREQUENCY_HZ, end + 1) + 1):
                actual = self.calculate(frequency, [item])["result"]
                if frequency < start or frequency > end:
                    expected = dict(status="outside_ranges", rule_index=None, nearest_frequency_hz=None,
                                    offset_hz=None, rounded_frequency_hz=None)
                else:
                    nearest = min(grid, key=lambda point: (abs(point-frequency), point))
                    offset = nearest - frequency
                    accepted = abs(offset) <= tolerance
                    status = ("unchanged" if offset == 0 else "rounded") if accepted else "outside_tolerance"
                    expected = dict(status=status, rule_index=0, nearest_frequency_hz=nearest,
                                    offset_hz=offset, rounded_frequency_hz=nearest if accepted else None)
                self.assertEqual(actual, expected, (frequency, item))
                checked += 1
        self.assertGreater(checked, 5000)

    def test_detached_exact_source_subset_preserves_any_recorded_unit(self):
        for unit in ("dBm", "s_meter_count", "arbitrary_recorded_unit"):
            source = candidate(level_unit=unit)
            before = copy.deepcopy(source)
            result = calculate_candidate_frequency_rounding("session", source, profile_json=document())
            self.assertEqual(set(result), {"session_id", "candidate", "profile", "frequency_unit", "method", "tie_break", "result"})
            self.assertEqual(result["candidate"], {
                "id": source.id, "source_stream_id": source.source_stream_id,
                "interval": to_primitive(source.interval), "center_frequency_hz": source.center_frequency_hz,
                "level_unit": unit,
            })
            self.assertEqual((result["frequency_unit"], result["method"], result["tie_break"]), ("Hz", "nearest_grid", "lower"))
            self.assertEqual(result["profile"], artifact())
            result["candidate"]["interval"]["started_at"] = "changed"
            result["profile"]["rules"][0]["step_hz"] = 999
            self.assertEqual(source, before)
            expected = calculate_candidate_frequency_rounding("session", source, profile_json=document())
            for state in CandidateState:
                source.state = state
                source.observation_ids = ("later-observation",)
                source.peak_level = float("nan")
                source.bandwidth_hz = object()
                source.score = float("inf")
                self.assertEqual(calculate_candidate_frequency_rounding("session", source, profile_json=document()), expected)

    def test_bad_source_gate_and_unbounded_scope_ids(self):
        for field, values in {
            "center_frequency_hz": [0, -1, MAX_FREQUENCY_HZ+1, True, 1.0, "1", None],
            "id": ["", " ", "\ud800", "😀"*129, None],
            "session_id": ["", " ", "\ud800", "other", None],
            "source_stream_id": ["", " ", "\ud800", None],
            "level_unit": ["", " ", "\ud800", None], "interval": [None, {}, "interval"],
        }.items():
            for value in values:
                source = candidate()
                setattr(source, field, value)
                with self.subTest(field=field, value=value), self.assertRaises(FrequencyRoundingIncompatibleError):
                    calculate_candidate_frequency_rounding("session", source, profile_json=document())
        for field in ("started_at", "ended_at"):
            for value in (None, "now", candidate().interval.started_at.replace(tzinfo=None),
                          candidate().interval.started_at.astimezone(timezone(timedelta(hours=1)))):
                source = candidate()
                setattr(source.interval, field, value)
                with self.subTest(field=field, value=value), self.assertRaises(FrequencyRoundingIncompatibleError):
                    calculate_candidate_frequency_rounding("session", source, profile_json=document())
        source = candidate()
        source.interval.ended_at = source.interval.started_at - timedelta(seconds=1)
        with self.assertRaises(FrequencyRoundingIncompatibleError):
            calculate_candidate_frequency_rounding("session", source, profile_json=document())
        source = candidate()
        source.session_id = "😀" * 256
        source.source_stream_id = "x" * 256
        result = calculate_candidate_frequency_rounding(source.session_id, source, profile_json=document())
        self.assertEqual(result["session_id"], source.session_id)
        self.assertEqual(result["candidate"]["source_stream_id"], source.source_stream_id)


class RoundingServiceMixin:
    def add(self, candidate_id="candidate-1", *, stream_index=0, **changes):
        stream = self.session.streams[stream_index]
        payload = candidate_payload(stream.id, candidate_id)
        payload.update(center_frequency_hz=102_500_003, level_unit=stream.level_unit)
        payload.update(changes)
        return self.service.create_candidate(self.session.id, payload)

    def calculate(self, candidate_id="candidate-1", profile_json=None):
        return self.service.candidate_frequency_rounding(self.session.id, request_body(candidate_id, profile_json))

    def snapshot(self):
        return copy.deepcopy((self.service.health(), self.service.events(), self.service.list_sessions(),
                              self.service.list_candidates(self.session.id), self.service.list_investigations(self.session.id),
                              self.service.list_sweeps(self.session.id), self.service.list_observations(self.session.id),
                              self.service.list_sweep_references(self.session.id), self.service.archive_capacity()))

    def test_readonly_success_failure_events_and_detached_result(self):
        original = self.add()
        self.add("too-large", center_frequency_hz=MAX_FREQUENCY_HZ+1)
        before = self.snapshot()
        subscriber_id, subscriber = self.service.subscribe(after=self.service.health()["last_event_id"])
        self.addCleanup(self.service.unsubscribe, subscriber_id)
        result = self.calculate()
        result["candidate"]["interval"]["started_at"] = "changed"
        result["profile"]["rules"][0]["step_hz"] = 999
        with self.assertRaises(FrequencyRoundingIncompatibleError):
            self.calculate("too-large")
        with self.assertRaises(CandidateNotFoundError):
            self.calculate("missing")
        with self.assertRaises(ValueError):
            self.calculate(profile_json=document(name=""))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.service.get_candidate(self.session.id, original.id), original)
        with self.assertRaises(queue.Empty):
            subscriber.get_nowait()
        self.add("next")
        self.assertEqual(self.service.health()["last_event_id"], before[0]["last_event_id"] + 1)

    def test_exact_ids_foreign_scope_and_capability(self):
        for identifier in (" encoded/+ candidate ", "😀"*128, "é", "e\u0301"):
            self.add(identifier)
            self.assertEqual(self.calculate(identifier)["candidate"]["id"], identifier)
        second = self.service.create_session(simulated_session_payload())
        self.service.create_candidate(second.id, candidate_payload(second.streams[0].id, "foreign"))
        for identifier in ("missing", "foreign", "encoded/+ candidate"):
            with self.subTest(identifier=identifier), self.assertRaises(CandidateNotFoundError):
                self.calculate(identifier)
        with self.assertRaises(SessionNotFoundError):
            self.service.candidate_frequency_rounding("missing", request_body())
        self.assertTrue(self.service.capabilities()["features"]["candidate_frequency_rounding"])

    def test_unit_state_and_handoff_remain_original(self):
        original = self.add()
        expected = self.calculate()
        receiver = self.add("receiver", stream_index=1)
        self.assertEqual(self.calculate(receiver.id)["candidate"]["level_unit"], "s_meter_count")
        self.service.start_session(self.session.id)
        investigation = self.service.enqueue_investigation(self.session.id, original.id,
                                                          {"target_stream_id": self.session.streams[1].id})
        self.assertEqual(investigation.target_frequency_hz, original.center_frequency_hz)
        before = self.snapshot()
        self.assertEqual(self.calculate(), expected)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.service.get_candidate(self.session.id, original.id).state, CandidateState.QUEUED)

    def test_calculation_and_source_validation_are_under_lock(self):
        self.add()
        original = calculate_candidate_frequency_rounding
        def checking(*args, **kwargs):
            self.assertTrue(self.service._lock._is_owned())
            return original(*args, **kwargs)
        with patch("radioinspector.session_service.calculate_candidate_frequency_rounding", side_effect=checking):
            self.calculate()
        self.service._candidates["candidate-1"].id = "changed"
        with self.assertRaises(FrequencyRoundingIncompatibleError):
            self.calculate()


class MemoryRoundingTests(RoundingServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=2)
        self.addCleanup(self.service.close)
        self.session = self.service.create_session(simulated_session_payload())


class SQLiteMemoryRoundingTests(RoundingServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(store=SQLiteSessionStore(":memory:"), history_limit=2)
        self.addCleanup(self.service.close)
        self.session = self.service.create_session(simulated_session_payload())


class SQLiteRoundingTests(RoundingServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.store = SQLiteSessionStore(self.path)
        self.service = SessionService(store=self.store, history_limit=2)
        self.addCleanup(lambda: self.service.close())
        self.session = self.service.create_session(simulated_session_payload())

    def test_no_sql_schema_changes_logical_dump_identical_success_and_failure(self):
        self.add()
        self.add("bad", center_frequency_hz=MAX_FREQUENCY_HZ+1)
        connection = self.store._connection
        before = (connection.total_changes, list(connection.iterdump()))
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            with patch.object(self.service, "list_candidates", side_effect=AssertionError("no scan")):
                self.calculate()
                self.calculate(profile_json=document([rule(1, 2, 1)]))
                self.calculate(profile_json=document([rule(tolerance=0)]))
                with self.assertRaises(FrequencyRoundingIncompatibleError):
                    self.calculate("bad")
                with self.assertRaises(ValueError):
                    self.calculate(profile_json="{}")
        finally:
            connection.set_trace_callback(None)
        self.assertEqual(statements, [])
        self.assertEqual((connection.total_changes, list(connection.iterdump())), before)

    def test_restart_at_full_capacity_keeps_old_candidate_derived_result(self):
        self.add()
        for number in range(110):
            self.add(f"newer-{number}")
        expected = self.calculate()
        size = self.service.archive_capacity()["database_bytes"]
        self.service.close()
        self.store = SQLiteSessionStore(self.path, max_database_bytes=size)
        self.service = SessionService(store=self.store, history_limit=2)
        before = (self.snapshot(), list(self.store._connection.iterdump()))
        self.assertEqual(self.service.archive_capacity()["state"], "full")
        self.assertEqual(self.calculate(), expected)
        self.assertEqual((self.snapshot(), list(self.store._connection.iterdump())), before)


class FrequencyRoundingHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(path))
        payload = simulated_session_payload()
        payload["id"] = "session/+ " + "x" * 130
        self.session = service.create_session(payload)
        for identifier, frequency, index in (("candidate-1", 102_500_003, 0),
                                             (" encoded/+ candidate ", 102_500_003, 0),
                                             ("bad", MAX_FREQUENCY_HZ+1, 0),
                                             ("receiver", 102_500_003, 1)):
            stream = self.session.streams[index]
            item = candidate_payload(stream.id, identifier)
            item.update(center_frequency_hz=frequency, level_unit=stream.level_unit)
            service.create_candidate(self.session.id, item)
        second = service.create_session(simulated_session_payload())
        service.create_candidate(second.id, candidate_payload(second.streams[0].id, "foreign"))
        size = service.archive_capacity()["database_bytes"]
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path, archive_max_bytes=size)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{quote(self.session.id, safe='')}/candidate-frequency-rounding"

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

    def raw_headers(self, headers, body=b"", *, shutdown=False, path=None):
        with socket.create_connection(self.server.server_address, timeout=5) as connection:
            connection.sendall((f"POST {path or self.root} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n" + headers + "\r\n").encode() + body)
            if shutdown:
                connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            result = response.status, json.loads(response.read())
            self.assertEqual(connection.recv(1), b"")
            return result

    def test_envelope_success_at_capacity_capability_and_exact_ids(self):
        service = self.server.session_service
        before = (service.health(), service.events(), service.archive_capacity(), list(service._store._connection.iterdump()))
        status, envelope = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(envelope["contract"], "openradioinspector-platform")
        self.assertEqual(envelope["contract_version"], 1)
        self.assertEqual(envelope["data"]["session_id"], self.session.id)
        self.assertEqual(envelope["data"]["result"], dict(status="rounded", rule_index=0,
            nearest_frequency_hz=102_500_000, offset_hz=-3, rounded_frequency_hz=102_500_000))
        self.assertEqual(envelope["data"]["profile"], artifact())
        self.assertEqual(before, (service.health(), service.events(), service.archive_capacity(), list(service._store._connection.iterdump())))
        self.assertEqual(before[2]["state"], "full")
        status, response = self.request(path="/api/v1/capabilities", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(response["data"]["features"]["candidate_frequency_rounding"])
        for identifier, unit in ((" encoded/+ candidate ", "dBm"), ("receiver", "s_meter_count")):
            status, response = self.request(request_body(identifier))
            self.assertEqual(status, 200)
            self.assertEqual(response["data"]["candidate"]["id"], identifier)
            self.assertEqual(response["data"]["candidate"]["level_unit"], unit)

    def test_malformed_body_duplicates_types_utf8_and_raw_artifact(self):
        valid = request_body()
        invalid = [b"", b"{", b"[]", b"null", b"true", b"{}", b"\xff", b"\xef\xbb\xbf{}",
                   json.dumps(dict(valid, extra=1)).encode(), json.dumps({"candidate_id": "candidate-1"}).encode(),
                   json.dumps({"profile_json": document()}).encode(),
                   (json.dumps(valid)[:-1] + ', "candidate_id": "candidate-1"}').encode(),
                   (json.dumps(valid)[:-1] + ', "profile_json": "{}"}').encode(),
                   (json.dumps(valid)[:-1] + ', "extra": {"x": 1, "x": 2}}').encode(),
                   (json.dumps(valid)[:-1] + ', "extra": NaN}').encode(),
                   (json.dumps(valid)[:-1] + ', "extra": "\\ud800"}').encode(),
                   b"[" * 1200 + b"0" + b"]" * 1200]
        for field, values in {"candidate_id": [None, True, 10, "", " ", "\ud800", "x"*129],
                              "profile_json": [None, True, 10, [], {}]}.items():
            invalid += [json.dumps(dict(valid, **{field: value})).encode() for value in values]
        raw = document()
        profiles = [raw.replace('"name": "Grid"', '"name": "Grid", "name": "Other"'),
                    raw.replace('"step_hz": 10', '"step_hz": 10, "step_hz": 20'),
                    raw.replace('"step_hz": 10', '"step_hz": 1e999'),
                    raw.replace('"step_hz": 10', '"step_hz": 10.0'),
                    raw.replace('"name": "Grid"', '"name": "\\ud800"'),
                    "[" * 1200 + "0" + "]" * 1200, raw + " " * MAX_PROFILE_BYTES]
        invalid += [json.dumps(request_body(profile_json=value)).encode() for value in profiles]
        before = self.server.session_service.health()
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                status, response = self.request(raw=raw)
                self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.server.session_service.health(), before)

    def test_no_queries_get_no_mutation_missing_foreign_incompatible(self):
        service = self.server.session_service
        before = (service.health(), service.events(), service.archive_capacity())
        for suffix in ("?", "?x=1", "?x=", "?&", "?candidate_id=candidate-1"):
            # Advertise a body but do not send it: query rejection must happen
            # before body reads. Sending it after headers races the server's
            # close and can reset the connection on Windows (WinError 10053).
            status, response = self.raw_headers("Content-Length: 100\r\n", path=self.root + suffix)
            self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.request(method="GET", raw=b"")[0], 404)
        for identifier, expected, code in (("missing", 404, "candidate_not_found"),
                                            ("foreign", 404, "candidate_not_found"),
                                            ("bad", 409, "frequency_rounding_incompatible")):
            status, response = self.request(request_body(identifier))
            self.assertEqual((status, response["error"]["code"]), (expected, code))
            self.assertNotIn("source_stream_id", json.dumps(response))
        status, response = self.request(path="/api/v1/sessions/missing/candidate-frequency-rounding")
        self.assertEqual((status, response["error"]["code"]), (404, "session_not_found"))
        self.assertEqual(before, (service.health(), service.events(), service.archive_capacity()))

    def test_body_limit_before_read_and_exact_boundary(self):
        for size in (str(MAX_ROUNDING_REQUEST_BYTES+1), "9"*5000):
            status, response = self.raw_headers(f"Content-Length: {size}\r\n")
            self.assertEqual((status, response["error"]["code"]), (413, "request_too_large"))
        raw = json.dumps(request_body()).encode()
        self.assertEqual(self.request(raw=raw + b" " * (MAX_ROUNDING_REQUEST_BYTES - len(raw)))[0], 200)
        status, response = self.raw_headers("Content-Length: 12\r\n", b"{}", shutdown=True)
        self.assertEqual((status, response["error"]["code"]), (400, "invalid_request"))

    def test_strict_content_length_and_transfer_encoding(self):
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
