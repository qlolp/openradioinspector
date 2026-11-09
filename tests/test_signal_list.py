"""Portable signal-list exchange between instruments and instances."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from radioinspector.contracts import to_primitive
from radioinspector.signal_list import (
    MAX_SIGNAL_LIST_BYTES, MAX_SIGNAL_LIST_ENTRIES, SIGNAL_LIST_FORMAT,
    SignalListError, build_signal_list, import_candidates, parse_signal_list,
)
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_investigations import BASE, candidate_payload
from tests.test_sweep_comparison import create_session

INTERVAL = {"started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"}


def entry(**changes):
    item = {"frequency_hz": 101_800_000, "bandwidth_hz": 12_500, "interval": dict(INTERVAL),
            "level_unit": "dBm", "peak_level": -50.5}
    item.update(changes)
    return item


def artifact(entries=None, **changes):
    document = {
        "format": SIGNAL_LIST_FORMAT, "format_version": 1, "name": "Список сигналов",
        "frequency_unit": "Hz", "entries": entries if entries is not None else [entry()],
    }
    document.update(changes)
    return document


def list_json(entries=None, **changes):
    return json.dumps(artifact(entries, **changes), ensure_ascii=False)


class SignalListContractTests(unittest.TestCase):
    def test_artifact_fields_format_and_names(self):
        parsed = parse_signal_list(list_json())
        self.assertEqual(parsed["name"], "Список сигналов")
        self.assertEqual(parsed["entries"], [entry()])
        for changes in ({"extra": 1}, {"format": "x"}, {"format_version": 2},
                        {"frequency_unit": "kHz"}, {"name": ""}, {"entries": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_signal_list(json.dumps(artifact(**changes)))
        many = [dict(entry(), frequency_hz=100_000_000 + i) for i in range(MAX_SIGNAL_LIST_ENTRIES + 1)]
        with self.assertRaises(ValueError):
            parse_signal_list(json.dumps(artifact(many)))

    def test_entry_fields_and_ranges(self):
        cases = [
            {"frequency_hz": True}, {"frequency_hz": -1}, {"frequency_hz": 2**53},
            {"bandwidth_hz": -1}, {"bandwidth_hz": "10"},
            {"interval": {}}, {"interval": dict(INTERVAL, extra=1)}, {"interval": dict(INTERVAL, started_at="")},
            {"level_unit": "dB"}, {"level_unit": None},
            {"peak_level": "x"}, {"peak_level": float("nan")},
            {"extra": 1},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_signal_list(json.dumps(artifact([entry(**changes)])))
        # Null bandwidth and peak are allowed: unknown measurements stay unknown.
        parsed = parse_signal_list(json.dumps(artifact([entry(bandwidth_hz=None, peak_level=None)])))
        self.assertIsNone(parsed["entries"][0]["bandwidth_hz"])

    def test_export_roundtrip_and_empty_refused(self):
        payload = candidate_payload("stream", "candidate-1")
        from radioinspector.contracts import Candidate
        candidate = Candidate.from_dict({**payload, "session_id": "session"})
        artifact_out = build_signal_list([candidate], "Экспорт")
        self.assertEqual(set(artifact_out), {"format", "format_version", "name", "frequency_unit", "entries"})
        raw = json.dumps(artifact_out, ensure_ascii=False)
        self.assertEqual(parse_signal_list(raw), artifact_out)
        with self.assertRaises(SignalListError):
            build_signal_list([], "Пусто")

    def test_export_obeys_importer_limits(self):
        from radioinspector.contracts import Candidate
        candidate = Candidate.from_dict(dict(candidate_payload('stream', 'c'), session_id='session'))
        with self.assertRaises(ValueError):
            build_signal_list([candidate] * 257, 'Too many')

    def test_import_never_converts_units(self):
        parsed = parse_signal_list(list_json())
        payloads = import_candidates("tiny", "dBm", parsed)
        self.assertEqual(payloads[0]["source_stream_id"], "tiny")
        self.assertEqual(payloads[0]["center_frequency_hz"], 101_800_000)
        self.assertEqual(payloads[0]["level_unit"], "dBm")
        self.assertNotIn("state", payloads[0], "imported signals always start as new")
        with self.assertRaises(SignalListError):
            import_candidates("tiny", "s_meter_count", parsed)
        with self.assertRaises(SignalListError):
            import_candidates("", "dBm", parsed)


class SignalListServiceMixin:
    def make_candidate(self, candidate_id="candidate-1", frequency=101_800_000):
        payload = candidate_payload(self.session.streams[0].id, candidate_id)
        payload["center_frequency_hz"] = frequency
        return self.service.create_candidate(self.session.id, payload)

    def test_invalid_tail_never_publishes_a_prefix(self):
        before_candidates = self.service.list_candidates(self.session.id)
        before_events = self.service.events()
        invalid = entry(interval={'started_at': INTERVAL['ended_at'], 'ended_at': INTERVAL['started_at']})
        with self.assertRaises(ValueError):
            self.service.signal_list_import(self.session.id, {
                'stream_id': self.session.streams[0].id, 'list_json': list_json([entry(), invalid])})
        self.assertEqual(self.service.list_candidates(self.session.id), before_candidates)
        self.assertEqual(self.service.events(), before_events)

    def test_export_import_roundtrip_via_service(self):
        self.make_candidate("candidate-1")
        self.make_candidate("candidate-2", 102_500_000)
        exported = self.service.signal_list_export(self.session.id, "Экспорт")
        self.assertEqual(exported["exported"], 2)
        raw = json.dumps(exported["list"], ensure_ascii=False)
        before = len(self.service.list_candidates(self.session.id))
        result = self.service.signal_list_import(
            self.session.id, {"stream_id": self.session.streams[0].id, "list_json": raw})
        self.assertEqual(result["imported"], 2)
        self.assertEqual(len(result["candidate_ids"]), 2)
        after = self.service.list_candidates(self.session.id)
        self.assertEqual(len(after), before + 2)
        imported = {item.id: item for item in after if item.id in set(result["candidate_ids"])}
        self.assertEqual(len(imported), 2)
        for candidate in imported.values():
            self.assertEqual(candidate.state.value, "new")
        self.assertEqual({c.center_frequency_hz for c in imported.values()},
                         {101_800_000, 102_500_000})

    def test_export_without_candidates_and_unit_mismatch(self):
        with self.assertRaises(SignalListError):
            self.service.signal_list_export(self.session.id, "Пусто")
        self.make_candidate()
        exported = self.service.signal_list_export(self.session.id, "Экспорт")
        raw = json.dumps(exported["list"], ensure_ascii=False)
        with self.assertRaises(ValueError):
            self.service.signal_list_import(self.session.id, {"stream_id": "foreign", "list_json": raw})
        # The helper makes both streams dBm; a mixed-unit session is built directly.
        from tests.test_platform import session_payload
        mixed = self.service.create_session(session_payload())
        with self.assertRaises(SignalListError):
            self.service.signal_list_import(
                mixed.id,
                {"stream_id": mixed.streams[1].id, "list_json": raw},
            )

    def test_capability(self):
        self.assertTrue(self.service.capabilities()["features"]["signal_list_exchange"])


class MemorySignalListTests(SignalListServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=100)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class SQLiteSignalListTests(SignalListServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class AtomicSignalListTests(unittest.TestCase):
    def test_sqlite_failure_rolls_back_records_and_events_before_publication(self):
        import sqlite3
        service = SessionService(store=SQLiteSessionStore(':memory:'))
        self.addCleanup(service.close)
        session = create_session(service)
        before = service.events()
        original = service._store._upsert_candidate
        calls = 0
        def fail_second(candidate):
            nonlocal calls
            calls += 1
            if calls == 2: raise sqlite3.OperationalError('injected disk failure')
            original(candidate)
        with patch.object(service._store, '_upsert_candidate', side_effect=fail_second):
            with self.assertRaises(sqlite3.OperationalError):
                service.signal_list_import(session.id, {'stream_id': session.streams[0].id,
                                                       'list_json': list_json([entry(), entry()])})
        self.assertEqual(service.list_candidates(session.id), [])
        self.assertEqual(service._store.load_candidates(), [])
        self.assertEqual(service.events(), before)
        self.assertEqual(service._store.events(), before)
        imported = service.signal_list_import(session.id, {'stream_id': session.streams[0].id,
                                                          'list_json': list_json([entry(), entry()])})
        self.assertEqual(imported['imported'], 2)
        self.assertEqual(len(service.list_candidates(session.id)), 2)


class SignalListHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(self.path))
        self.session = create_session(service)
        self.stream = self.session.streams[0].id
        payload = candidate_payload(self.stream, "candidate-1")
        service.create_candidate(self.session.id, payload)
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/signal-list"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path,
                               body=None if body is None else json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_import_rejects_oversized_body_before_reading_it(self):
        from radioinspector.signal_list import MAX_IMPORT_REQUEST_BYTES
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request('POST', self.root + '/import', headers={
                'Content-Type': 'application/json', 'Content-Length': str(MAX_IMPORT_REQUEST_BYTES + 1)})
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            self.assertEqual(json.loads(response.read())['error']['code'], 'request_too_large')
        finally:
            connection.close()

    def test_export_and_import_over_http(self):
        status, envelope = self.request("GET", self.root + "/export?name=Test")
        self.assertEqual(status, 200)
        artifact = envelope["data"]["list"]
        self.assertEqual(artifact["name"], "Test")
        self.assertEqual(envelope["data"]["exported"], 1)
        raw = json.dumps(artifact, ensure_ascii=False)
        status, envelope = self.request("POST", self.root + "/import",
                                        {"stream_id": self.stream, "list_json": raw})
        self.assertEqual(status, 201)
        self.assertEqual(envelope["data"]["imported"], 1)
        self.assertEqual(len(envelope["data"]["candidate_ids"]), 1)

    def test_mixed_unit_session_roundtrips_each_selected_stream(self):
        from tests.test_platform import session_payload
        service = self.server.session_service
        session = service.create_session(session_payload())
        for index, stream in enumerate(session.streams):
            payload = candidate_payload(stream.id, f'mixed-{index}')
            payload['level_unit'] = stream.level_unit
            payload['peak_level'] = -50 if index == 0 else 12
            service.create_candidate(session.id, payload)
        root = f'/api/v1/sessions/{session.id}/signal-list'
        for stream in session.streams:
            status, result = self.request('GET', root + '/export?stream_id=' + stream.id)
            self.assertEqual(status, 200)
            self.assertEqual(result['data']['exported'], 1)
            self.assertEqual(result['data']['list']['entries'][0]['level_unit'], stream.level_unit)
            status, imported = self.request('POST', root + '/import', {
                'stream_id': stream.id, 'list_json': json.dumps(result['data']['list'])})
            self.assertEqual(status, 201)
            self.assertEqual(imported['data']['imported'], 1)
        status, _ = self.request('GET', root + '/export?stream_id=foreign')
        self.assertEqual(status, 400)

    def test_error_statuses(self):
        cases = [
            ("POST", self.root + "/import", {"stream_id": "foreign", "list_json": "{}"}, 400, "invalid_request"),
            ("POST", self.root + "/import", {"stream_id": self.stream, "list_json": "{}"}, 400, "invalid_request"),
            # A level unit that differs from the stream is refused; no conversion.
            ("POST", self.root + "/import",
             {"stream_id": self.stream,
              "list_json": json.dumps(artifact([entry(level_unit="s_meter_count")]))},
             409, "signal_list_error"),
        ]
        for method, path, body, expected, error in cases:
            with self.subTest(path=path):
                code, envelope = self.request(method, path, body)
                self.assertEqual(code, expected)
                self.assertEqual(envelope["error"]["code"], error)
        status, envelope = self.request("GET", "/api/v1/sessions/no-such-session/signal-list/export")
        self.assertEqual(status, 404)
        self.assertEqual(envelope["error"]["code"], "session_not_found")

    def test_capability_advertised(self):
        status, envelope = self.request("GET", "/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(envelope["data"]["features"]["signal_list_exchange"])


if __name__ == "__main__":
    unittest.main()
