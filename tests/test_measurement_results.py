"""Measurement results bound to an explicit methodology, code apart from values."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from contextlib import closing

from radioinspector.contracts import to_primitive
from radioinspector.measurement_results import (
    METHODOLOGIES, RESULT_CODES, describe_catalog, make_result_record,
    validate_result_payload,
)
from radioinspector.session_service import SessionNotFoundError, SessionService
from radioinspector.session_store import SCHEMA_VERSION, SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session

INTERVAL = {"started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"}


def payload(methodology="beta2_bandwidth", result_code="norm", values=None, **changes):
    if values is None:
        fields = METHODOLOGIES.get(methodology, {}).get("fields", ())
        values = ({} if result_code == "not_measured"
                  else {name: index + 1.0 for index, name in enumerate(fields)})
    body = {
        "methodology": methodology, "result_code": result_code,
        "frequency_hz": 101_800_000, "interval": dict(INTERVAL), "values": values,
    }
    body.update(changes)
    return body


class ResultContractTests(unittest.TestCase):
    def test_exact_shape_methodology_code_frequency_interval(self):
        invalid = [None, [], True, "x", {}, dict(payload(), extra=1)]
        for field in payload():
            body = payload()
            del body[field]
            invalid.append(body)
        invalid += [payload(methodology=value)
                    for value in (None, 1, "", "unknown", "MEAS_B2_RESULT")]
        invalid += [payload(result_code=value)
                    for value in (None, 1, "", "ok", "norma", "NORM")]
        invalid += [payload(frequency_hz=value)
                    for value in (None, True, 1.0, "1", -1, 2**53)]
        invalid += [payload(interval=value)
                    for value in (None, {}, {"started_at": "x"},
                                  dict(INTERVAL, extra="x"),
                                  dict(INTERVAL, started_at=None),
                                  dict(INTERVAL, ended_at=""))]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises((ValueError, TypeError)):
                validate_result_payload(body)
        parsed = validate_result_payload(payload())
        self.assertEqual(parsed["methodology"], "beta2_bandwidth")
        self.assertEqual(set(parsed), {"methodology", "result_code", "frequency_hz",
                                       "interval", "values"})

    def test_values_match_methodology_and_not_measured_is_empty(self):
        for key in METHODOLOGIES:
            fields = METHODOLOGIES[key]["fields"]
            for code in ("norm", "not_norm"):
                parsed = validate_result_payload(payload(key, code))
                self.assertEqual(set(parsed["values"]), set(fields))
            parsed = validate_result_payload(payload(key, "not_measured"))
            self.assertEqual(parsed["values"], {})
        # Missing, extra and wrong-typed values are refused.
        fields = METHODOLOGIES["beta2_bandwidth"]["fields"]
        cases = [dict(values={})]
        cases.append({**{f: 1.0 for f in fields}, "extra": 1.0})
        cases.append({f: (1.0 if f != fields[0] else "x") for f in fields})
        cases.append({f: (1.0 if f != fields[0] else float("nan")) for f in fields})
        cases.append({f: (1.0 if f != fields[0] else float("inf")) for f in fields})
        cases.append({f: (1.0 if f != fields[0] else True) for f in fields})
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_result_payload(payload(values=values))
        # not_measured never carries values.
        with self.assertRaises(ValueError):
            validate_result_payload(payload(result_code="not_measured", values={"bandwidth_hz": 1.0}))

    def test_interval_validation_and_utc_normalization(self):
        for started, ended in [('not-a-date', INTERVAL['ended_at']),
                               ('2026-11-08T12:00:00', INTERVAL['ended_at']),
                               (INTERVAL['ended_at'], INTERVAL['started_at'])]:
            with self.subTest(started=started), self.assertRaises(ValueError):
                validate_result_payload(payload(interval={'started_at': started, 'ended_at': ended}))
        parsed = validate_result_payload(payload(interval={
            'started_at': '2026-11-08T15:00:00+03:00', 'ended_at': '2026-11-08T15:00:01+03:00'}))
        self.assertEqual(parsed['interval'], INTERVAL)

    def test_catalog_lists_implemented_methodologies(self):
        catalog = describe_catalog()
        self.assertEqual(len(catalog), len(METHODOLOGIES))
        for entry in catalog:
            spec = METHODOLOGIES[entry["methodology"]]
            self.assertEqual(entry["title"], spec["title"])
            self.assertEqual(entry["original_table"], spec["original_table"])
            self.assertEqual(entry["fields"], list(spec["fields"]))
            self.assertEqual(entry["result_codes"], list(RESULT_CODES))
        self.assertIn("beta2_bandwidth", {entry["methodology"] for entry in catalog})

    def test_record_binds_provenance_and_detaches_values(self):
        record = make_result_record("session", "stream", payload(), "result-1")
        self.assertEqual(record["session_id"], "session")
        self.assertEqual(record["stream_id"], "stream")
        self.assertEqual(record["id"], "result-1")
        self.assertIn("created_at", record)
        source = payload(values={"bandwidth_hz": [1]})
        with self.assertRaises(ValueError):
            make_result_record("session", "stream", source, "result-2")


class ResultServiceMixin:
    def save(self, body=None, stream=None):
        body = dict(body or payload())
        body["stream_id"] = stream or self.session.streams[0].id
        from unittest.mock import patch
        # Hosted Windows may assign the same wall-clock tick to consecutive saves.
        self.saved_count = getattr(self, 'saved_count', 0) + 1
        stamp = datetime(2026, 11, 8, tzinfo=timezone.utc) + timedelta(seconds=self.saved_count)
        with patch('radioinspector.measurement_results.utc_now', return_value=stamp):
            return self.service.save_measurement_result(self.session.id, body)

    def test_saves_and_lists_in_order_with_methodology_filter(self):
        first = self.save(payload("beta2_bandwidth", "norm"))
        second = self.save(payload("control_bandwidth", "not_norm"))
        third = self.save(payload("beta2_bandwidth", "not_measured"))
        items = self.service.list_measurement_results(self.session.id)
        self.assertEqual([item["id"] for item in items],
                         [first["id"], second["id"], third["id"]])
        only = self.service.list_measurement_results(self.session.id, methodology="beta2_bandwidth")
        self.assertEqual({item["id"] for item in only}, {first["id"], third["id"]})
        self.assertEqual(items[0]["values"]["bandwidth_hz"], 1.0)
        self.assertEqual(items[1]["result_code"], "not_norm")
        self.assertEqual(items[2]["values"], {})

    def test_limit_returns_newest_window_in_chronological_order(self):
        saved = [self.save() for _ in range(3)]
        items = self.service.list_measurement_results(self.session.id, limit=2)
        self.assertEqual([r['id'] for r in items], [r['id'] for r in saved[-2:]])

    def test_values_are_detached_and_archive_survives_restart(self):
        record = self.save(payload(values={"bandwidth_hz": 7.5, "lower_edge_hz": 1.0,
                                           "upper_edge_hz": 2.0, "center_hz": 1.5}))
        self.assertEqual(record["values"]["bandwidth_hz"], 7.5)
        source = payload()
        saved = self.save(source)
        source["values"]["bandwidth_hz"] = 999
        self.assertEqual(saved["values"]["bandwidth_hz"], 1.0)
        if self.service._store is None:
            return  # Memory assertions above are complete; SQLite cases also verify restart.
        path = self.path
        self.service.close()
        restored = SessionService(store=SQLiteSessionStore(path))
        self.addCleanup(restored.close)
        items = restored.list_measurement_results(self.session.id)
        self.assertEqual({item["id"] for item in items}, {record["id"], saved["id"]})
        self.assertEqual(items[0]["values"]["bandwidth_hz"], 7.5)

    def test_foreign_stream_session_and_unknown_methodology(self):
        other = create_session(self.service)
        with self.assertRaises(ValueError):
            self.save(stream=other.streams[0].id)
        with self.assertRaises(ValueError):
            self.service.save_measurement_result("no-such-session", payload())
        with self.assertRaises(ValueError):
            self.service.list_measurement_results(self.session.id, methodology="unknown")
        with self.assertRaises(SessionNotFoundError):
            self.service.list_measurement_results("no-such-session")

    def test_capability(self):
        features = self.service.capabilities()["features"]
        self.assertTrue(features["measurement_results"])
        self.assertTrue(features["measurement_methodology_catalog"])


class MemoryResultTests(ResultServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=50)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class SQLiteResultTests(ResultServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class ResultMigrationTests(unittest.TestCase):
    def test_sqlite_startup_does_not_materialize_all_measurement_results(self):
        from unittest.mock import patch
        store = SQLiteSessionStore(':memory:')
        with patch.object(store, 'load_measurement_results', side_effect=AssertionError('unbounded read')):
            service = SessionService(store=store)
            service.close()

    def test_v3_upgrade_preserves_existing_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'v3.sqlite')
            service = SessionService(store=SQLiteSessionStore(path))
            session = create_session(service)
            service.close()
            import sqlite3
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute('DROP TABLE sweep_ingestion')
                connection.execute('ALTER TABLE investigations DROP COLUMN listening_started_at')
                connection.execute('DROP TABLE measurement_results')
                connection.execute('PRAGMA user_version = 3')
            service = SessionService(store=SQLiteSessionStore(path))
            try:
                self.assertEqual(service.get_session(session.id).id, session.id)
                saved = service.save_measurement_result(session.id, dict(payload(), stream_id=session.streams[0].id))
                self.assertEqual(service.list_measurement_results(session.id)[0]['id'], saved['id'])
            finally:
                service.close()


class ResultHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(self.path))
        self.session = create_session(service)
        self.stream = self.session.streams[0].id
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/measurement-results"

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

    def test_save_and_list_over_http(self):
        body = dict(payload(), stream_id=self.stream)
        status, envelope = self.request("POST", self.root, body)
        self.assertEqual(status, 201)
        saved = envelope["data"]
        self.assertEqual(saved["methodology"], "beta2_bandwidth")
        self.assertEqual(saved["stream_id"], self.stream)
        status, envelope = self.request("GET", self.root + "?methodology=beta2_bandwidth")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in envelope["data"]], [saved["id"]])

    def test_catalog_over_http(self):
        status, envelope = self.request("GET", "/api/v1/measurement-catalog")
        self.assertEqual(status, 200)
        ids = {entry["methodology"] for entry in envelope["data"]}
        self.assertIn("beta2_bandwidth", ids)
        self.assertIn("control_bandwidth", ids)

    def test_error_statuses(self):
        cases = [
            (dict(payload(), stream_id="foreign"), 400, "invalid_request"),
            (dict(payload(result_code="norm"), stream_id=self.stream,
                  values={}), 400, "invalid_request"),
            ({}, 400, "invalid_request"),
        ]
        for body, status, error in cases:
            with self.subTest(body=body):
                code, envelope = self.request("POST", self.root, body)
                self.assertEqual(code, status)
                self.assertEqual(envelope["error"]["code"], error)
        code, envelope = self.request(
            "GET", "/api/v1/sessions/no-such-session/measurement-results")
        self.assertEqual(code, 404)
        self.assertEqual(envelope["error"]["code"], "session_not_found")

    def test_capability_advertised(self):
        status, envelope = self.request("GET", "/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(envelope["data"]["features"]["measurement_results"])


if __name__ == "__main__":
    unittest.main()
