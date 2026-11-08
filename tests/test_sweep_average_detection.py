"""Synthetic-only tests for read-only averaged-sweep change detection."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.sweep_average import (
    calculate_sweep_average_change_detection,
    validate_change_detection_request,
)
from radioinspector.web.server import create_server
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
        from radioinspector.contracts import Sweep

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
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=path)
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

    def request(self, payload=None, path=None, raw=None):
        body = raw if raw is not None else json.dumps(
            _request() if payload is None else payload,
        ).encode()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", path or self.path, body=body,
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
        capabilities = service.capabilities()
        self.assertTrue(capabilities["features"]["averaged_sweep_change_detection"])

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


if __name__ == "__main__":
    unittest.main()
