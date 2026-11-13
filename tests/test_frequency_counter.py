"""Built-in frequency counter: honest resolution bound, read-only on archived sweeps."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from radioinspector.contracts import Sweep
from radioinspector.frequency_counter import (
    COUNTER_METHODOLOGY, MAX_COUNTER_ID_LENGTH, CounterIncompatibleError,
    calculate_frequency_counter, validate_counter_request,
)
from radioinspector.measurement_results import METHODOLOGIES
from radioinspector.session_service import (
    SessionNotFoundError, SessionService, SweepNotFoundError,
)
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session, measurement


def request_body(**changes):
    body = {"sweep_id": "sweep-0", "resolution_hz": 100, "start_hz": 100, "stop_hz": 400}
    body.update(changes)
    return body


def sweep(frequencies=None, levels=None, sweep_id="sweep-0"):
    body = measurement("session", "stream", 0)
    body["id"] = sweep_id
    if frequencies is not None:
        body["frequencies_hz"] = frequencies
    if levels is not None:
        body["levels"] = levels
    return Sweep.from_dict(body)


class CounterContractTests(unittest.TestCase):
    def test_request_exact_shape_integer_bounds_utf8(self):
        invalid = [None, [], True, {}, dict(request_body(), extra=1), {"sweep_id": "x"}]
        for field in request_body():
            body = request_body()
            del body[field]
            invalid.append(body)
        invalid += [request_body(sweep_id=value)
                    for value in (None, True, 1, "", " \t\n", "x" * (MAX_COUNTER_ID_LENGTH + 1), "\ud800")]
        invalid += [request_body(resolution_hz=value)
                    for value in (None, True, 0, -1, 1.0, "1", [], {})]
        invalid += [request_body(start_hz=value) for value in (None, True, -1, 2**53, 1.0)]
        invalid += [request_body(stop_hz=value) for value in (None, True, -1, 2**53, 1.0)]
        invalid += [request_body(start_hz=400, stop_hz=400), request_body(start_hz=500, stop_hz=400)]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises((ValueError, TypeError)):
                validate_counter_request(body)
        parsed = validate_counter_request(request_body())
        self.assertEqual(parsed, {"sweep_id": "sweep-0", "resolution_hz": 100,
                                  "start_hz": 100, "stop_hz": 400})

    def test_parabolic_estimate_clamped_and_honest_uncertainty(self):
        # Uniform raster 100 Hz; peak at bin 2 leaning left (right neighbour lower).
        source = sweep(frequencies=[100, 200, 300, 400, 500],
                       levels=[-90.0, -80.0, -50.0, -52.0, -90.0])
        result = calculate_frequency_counter("session", source, resolution_hz=10,
                                             start_hz=100, stop_hz=500)
        self.assertEqual(result["method"], "parabolic")
        self.assertEqual(result["peak_index"], 2)
        self.assertEqual(result["raster_hz"], 100)
        self.assertEqual(result["peak_frequency_hz"], 300)
        # shift = 0.5 * (y_left - y_right) / (y_left - 2*y_center + y_right)
        #       = 0.5 * (-80 - (-52)) / (-80 + 100 - 52) = 14/32 = 0.4375
        self.assertAlmostEqual(result["measured_frequency_hz"], 343.75, places=9)
        self.assertEqual(result["uncertainty_hz"], 50.0, "bound is max(resolution, raster)/2")
        self.assertEqual(result["measurement_methodology"], COUNTER_METHODOLOGY)
        self.assertEqual(set(result["values"]),
                         set(METHODOLOGIES[COUNTER_METHODOLOGY]["fields"]))
        self.assertEqual(result["values"]["resolution_hz"], 10.0)
        self.assertEqual(result["sweep"]["id"], "sweep-0")
        self.assertEqual(result["window"], {"start_hz": 100, "stop_hz": 500})

    def test_peak_bin_fallback_edges_ties_and_repeated(self):
        # Peak at the window edge: no interpolation.
        source = sweep(frequencies=[100, 200, 300, 400], levels=[-50.0, -60.0, -70.0, -80.0])
        result = calculate_frequency_counter("session", source, resolution_hz=10,
                                             start_hz=100, stop_hz=400)
        self.assertEqual(result["method"], "peak_bin")
        self.assertEqual(result["measured_frequency_hz"], 100.0)
        self.assertEqual(result["raster_hz"], 100)
        self.assertEqual(result["uncertainty_hz"], 50.0, "edge peaks retain the recorded spacing bound")
        # Ties resolve to the first maximum: the leftmost peak is an edge peak.
        source = sweep(frequencies=[100, 200, 300], levels=[-50.0, -50.0, -60.0])
        result = calculate_frequency_counter("session", source, resolution_hz=10,
                                             start_hz=100, stop_hz=300)
        self.assertEqual(result["peak_index"], 0, "first maximum on ties")
        self.assertEqual(result["method"], "peak_bin")
        # Repeated frequencies: raster is not uniform, no interpolation.
        source = sweep(frequencies=[100, 200, 200, 300], levels=[-80.0, -50.0, -51.0, -80.0])
        result = calculate_frequency_counter("session", source, resolution_hz=10,
                                             start_hz=100, stop_hz=300)
        self.assertEqual(result["method"], "peak_bin")
        self.assertEqual(result["raster_hz"], 100)
        # A steep asymmetric slope stays inside the peak bin span.
        source = sweep(frequencies=[100, 200, 300], levels=[-100.0, -1.0, -1000.0])
        result = calculate_frequency_counter("session", source, resolution_hz=1,
                                             start_hz=100, stop_hz=300)
        self.assertEqual(result["method"], "parabolic")
        self.assertGreater(result["measured_frequency_hz"], 150.0)
        self.assertLess(result["measured_frequency_hz"], 250.0)

    def test_non_dbm_units_and_nonuniform_uncertainty(self):
        source = sweep(frequencies=[100, 200, 500], levels=[-60.0, -50.0, -60.0])
        result = calculate_frequency_counter('session', source, resolution_hz=1, start_hz=100, stop_hz=500)
        self.assertEqual(result['method'], 'peak_bin')
        self.assertEqual(result['uncertainty_hz'], 150.0)
        source.level_unit = 's_meter_count'
        with self.assertRaises(CounterIncompatibleError):
            calculate_frequency_counter('session', source, resolution_hz=1, start_hz=100, stop_hz=500)

    def test_window_without_bins_and_bad_sweeps_rejected(self):
        source = sweep(frequencies=[100, 200, 300], levels=[-60.0, -50.0, -60.0])
        with self.assertRaises(CounterIncompatibleError):
            calculate_frequency_counter("session", source, resolution_hz=10,
                                        start_hz=500, stop_hz=600)
        # Unsorted or negative grids are refused by the contract before the
        # counter sees them; both layers reject with ValueError subclasses.
        for changes in ({"frequencies_hz": [300, 200, 100]}, {"frequencies_hz": [100, -1, 300]}):
            bad = measurement("session", "stream", 0)
            bad.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Sweep.from_dict(bad)


class CounterServiceMixin:
    def measure(self, **changes):
        return self.service.frequency_counter(self.session.id, request_body(**changes))

    def test_measures_archive_sweep_without_writes(self):
        self.service.ingest_sweep(measurement(self.session.id, self.session.streams[0].id, 0,
                                              frequencies_hz=[100, 200, 300, 400],
                                              levels=[-90.0, -80.0, -50.0, -52.0]))
        result = self.measure()
        self.assertEqual(result["method"], "parabolic")
        self.assertEqual(result["peak_index"], 2)
        before = (self.service.health(), self.service.events())
        self.measure()
        self.assertEqual((self.service.health(), self.service.events()), before)

    def test_unknown_sweep_and_session(self):
        self.service.ingest_sweep(measurement(self.session.id, self.session.streams[0].id, 0))
        with self.assertRaises(SweepNotFoundError):
            self.measure(sweep_id="sweep-404")
        with self.assertRaises(SessionNotFoundError):
            self.service.frequency_counter("no-such-session", request_body())

    def test_capability(self):
        self.assertTrue(self.service.capabilities()["features"]["frequency_counter"])


class MemoryCounterTests(CounterServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=50)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class SQLiteCounterTests(CounterServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class CounterHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(self.path))
        self.session = create_session(service)
        service.ingest_sweep(measurement(self.session.id, self.session.streams[0].id, 0,
                                         frequencies_hz=[100, 200, 300, 400],
                                         levels=[-90.0, -80.0, -50.0, -52.0]))
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/frequency-counter"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, body, *, path=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", path or self.root, body=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_measures_over_http(self):
        status, envelope = self.request(request_body())
        self.assertEqual(status, 200)
        data = envelope["data"]
        self.assertEqual(data["method"], "parabolic")
        self.assertEqual(data["peak_frequency_hz"], 300)
        self.assertEqual(data["measurement_methodology"], COUNTER_METHODOLOGY)

    def test_error_statuses(self):
        cases = [
            (request_body(sweep_id="sweep-404"), 404, "sweep_not_found"),
            (request_body(start_hz=500, stop_hz=600), 409, "counter_incompatible"),
            ({"sweep_id": "x"}, 400, "invalid_request"),
        ]
        for body, status, error in cases:
            with self.subTest(body=body):
                code, envelope = self.request(body)
                self.assertEqual(code, status)
                self.assertEqual(envelope["error"]["code"], error)
        code, envelope = self.request(request_body(),
                                      path="/api/v1/sessions/no-such-session/frequency-counter")
        self.assertEqual(code, 404)
        self.assertEqual(envelope["error"]["code"], "session_not_found")

    def test_query_parameters_rejected(self):
        code, envelope = self.request(request_body(), path=self.root + "?x=1")
        self.assertEqual(code, 400)
        self.assertEqual(envelope["error"]["code"], "invalid_request")

    def test_capability_advertised(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", "/api/v1/capabilities")
            response = connection.getresponse()
            envelope = json.loads(response.read())
        finally:
            connection.close()
        self.assertTrue(envelope["data"]["features"]["frequency_counter"])


if __name__ == "__main__":
    unittest.main()
