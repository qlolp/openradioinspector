"""Common-archive sample occupancy: synthetic data only, no hardware."""
import copy
import http.client
import json
import shutil
import subprocess
import threading
import unittest
from pathlib import Path

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.sweep_context import ContextIncompatibleError
from radioinspector.sweep_occupancy import validate_occupancy_request
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session, measurement
from tests.test_sweep_context import interval


def request(**changes):
    return dict(sweep_id="sweep-1", before=2, after=2, threshold_dbm=-60, **changes)


class OccupancyTests(unittest.TestCase):
    def make_service(self, persistent=False):
        service = SessionService(store=SQLiteSessionStore(":memory:") if persistent else None)
        self.addCleanup(service.close)
        session = create_session(service)
        for number, (start, duration, levels) in enumerate([
            (0, 2_000_000, [-60, -61, -59, -60]),
            (1_000_000, 2_000_000, [-60, -59, -58, -61]),
            (5_000_000, 1_000_000, [-61, -59, -57, -59]),
        ]):
            service.ingest_sweep(measurement(session.id, session.streams[0].id, number,
                interval=interval(start, duration=duration), levels=levels))
        return service, session

    def test_counts_strict_threshold_gaps_and_memory_sqlite_parity(self):
        results = []
        for persistent in (False, True):
            service, session = self.make_service(persistent)
            before = copy.deepcopy((service.health(), service.events(), service.list_sweeps(session.id)))
            result = service.sweep_occupancy(session.id, request())
            self.assertEqual(result["missing"], {"before": 1, "after": 1})
            self.assertFalse(result["complete"])
            self.assertEqual([row["id"] for row in result["items"]], ["sweep-0", "sweep-1", "sweep-2"])
            stats = result["occupancy"]
            self.assertEqual(stats["exceedance_counts"], [0, 2, 3, 1])
            self.assertEqual(stats["sample_fractions"], [0, 2/3, 1, 1/3])
            self.assertEqual(stats["sample_count"], 3)
            self.assertEqual(stats["acquisition_span_seconds"], 6)
            self.assertEqual(stats["acquisition_covered_seconds"], 4)
            self.assertEqual(stats["acquisition_gap_seconds"], 2)
            self.assertEqual(stats["acquisition_gap_count"], 1)
            self.assertIsNone(stats["rf_time_occupancy"])
            self.assertEqual((service.health(), service.events(), service.list_sweeps(session.id)), before)
            results.append(stats)
            result["items"][0]["levels"][0] = 999
            self.assertEqual(service.sweep_occupancy(session.id, request())["occupancy"], stats)
        self.assertEqual(*results)

    def test_one_sweep_zero_duration_duplicate_bins_and_adjacent_intervals(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = create_session(service)
        for number in range(3):
            service.ingest_sweep(measurement(session.id, session.streams[0].id, number,
                frequencies_hz=[100, 100], levels=[-61, -59],
                interval=interval(number * 1_000_000, duration=0 if number == 2 else 1_000_000)))
        result = service.sweep_occupancy(session.id, request())
        self.assertEqual(result["occupancy"]["exceedance_counts"], [0, 3])
        self.assertEqual(result["occupancy"]["acquisition_gap_seconds"], 0)
        self.assertEqual(result["occupancy"]["acquisition_covered_seconds"], 2)
        result = service.sweep_occupancy(session.id, {**request(), "sweep_id": "sweep-2", "before": 0, "after": 0})
        self.assertEqual(result["occupancy"]["sample_fractions"], [0, 1])
        self.assertEqual(result["occupancy"]["sample_count"], 1)
        self.assertEqual(result["occupancy"]["acquisition_span_seconds"], 0)

    def test_request_validation(self):
        for value in (True, None, "-60", [], float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_occupancy_request({**request(), "threshold_dbm": value})
        for value in ({}, {**request(), "extra": 1}, {**request(), "before": True},
                      {**request(), "after": 17}, {**request(), "sweep_id": ""}):
            with self.assertRaises(ValueError):
                validate_occupancy_request(value)

    def test_identical_timestamps_count_samples_without_double_counting_time(self):
        service = SessionService(store=SQLiteSessionStore(":memory:"))
        self.addCleanup(service.close)
        session = create_session(service)
        for number in range(3):
            service.ingest_sweep(measurement(session.id, session.streams[0].id, number,
                interval=interval(0, duration=1_000_000)))
        connection = service._store._connection
        before = connection.total_changes, list(connection.iterdump())
        stats = service.sweep_occupancy(session.id, request())["occupancy"]
        self.assertEqual(stats["sample_count"], 3)
        self.assertEqual(stats["acquisition_span_seconds"], 1)
        self.assertEqual(stats["acquisition_covered_seconds"], 1)
        self.assertEqual(stats["acquisition_gap_count"], 0)
        self.assertEqual((connection.total_changes, list(connection.iterdump())), before)

    def test_incompatible_window_rejected_not_silently_filtered(self):
        for changes in ({"settings": {"rbw_hz": 20_000}},
                        {"frequencies_hz": [101, 200, 300, 400]}):
            service, session = self.make_service()
            service.ingest_sweep(measurement(session.id, session.streams[0].id, 3,
                interval=interval(7_000_000), **changes))
            with self.subTest(changes=changes), self.assertRaises(ContextIncompatibleError):
                service.sweep_occupancy(session.id, {**request(), "sweep_id": "sweep-2"})

    def test_raw_s_meter_counts_rejected_at_ingest(self):
        service, session = self.make_service()
        with self.assertRaises(ValueError):
            service.ingest_sweep(measurement(session.id, session.streams[0].id, 3,
                level_unit="s_meter_count"))


class OccupancyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server(host="127.0.0.1", port=0)
        session = create_session(self.server.session_service)
        self.server.session_service.ingest_sweep(measurement(session.id, session.streams[0].id, 1))
        self.path = f"/api/v1/sessions/{session.id}/sweep-occupancy"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def post(self, body, suffix=""):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", self.path + suffix, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_contract_errors_and_read_only(self):
        service = self.server.session_service
        before = service.health(), service.events()
        status, body = self.post(json.dumps(request()))
        self.assertEqual(status, 200)
        self.assertEqual(body["contract_version"], 1)
        self.assertEqual(body["data"]["occupancy"]["exceedance_counts"], [0, 0, 0, 1])
        self.assertEqual(self.post(json.dumps(request()), "?")[0], 400)
        self.assertEqual(self.post(json.dumps({**request(), "sweep_id": "missing"}))[0], 404)
        self.assertEqual(self.post(json.dumps(request())[:-1] + ',"before":0}')[0], 400)
        self.assertEqual(self.post(json.dumps(request())[:-1] + ',"bad":NaN}')[0], 400)
        self.assertEqual(self.post(b" " * 8193)[0], 413)
        self.assertEqual((service.health(), service.events()), before)

    def test_windows_client_uses_real_core_and_retains_export_sources(self):
        from radioinspector.windows.client import PlatformClient
        from radioinspector.windows.sweep_occupancy import validate_occupancy, save_occupancy_file
        import tempfile
        service = self.server.session_service
        session = service.list_sessions()[0]
        session_id = session['id'] if isinstance(session, dict) else session.id
        client = PlatformClient(f'http://127.0.0.1:{self.server.server_address[1]}')
        result = client.sweep_occupancy(session_id, 'sweep-1', 2, 2, -60)
        source = dict(result['items'][0], session_id=session_id, stream_id=result['stream_id'], frequencies_hz=result['frequencies_hz'])
        validate_occupancy(result, (session_id, result['stream_id']), source, 2, 2, -60)
        self.assertEqual(result['occupancy']['exceedance_counts'], [0, 0, 0, 1])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'occupancy.json'
            save_occupancy_file(path, result, result.data_json)
            self.assertEqual(json.loads(path.read_text())['data'], result)

    @unittest.skipUnless(shutil.which("node"), "Node required for real WebUI/HTTP integration")
    def test_webui_with_real_core_and_synthetic_archive(self):
        from tests.sweep_context_smoke_server import _populate
        service = self.server.session_service
        _populate(service)
        before = copy.deepcopy((service.health(), service.events()))
        root = Path(__file__).resolve().parents[1]
        address = "http://%s:%s" % self.server.server_address
        result = subprocess.run([
            shutil.which("node"), str(root / "tests/webui_sweep_context.cjs"),
            str(root / "radioinspector/web/static/sweep-context.js"),
            str(root / "radioinspector/web/static/platform.js"), address,
        ], capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Web HTTP occupancy smoke passed", result.stdout)
        self.assertEqual((service.health(), service.events()), before)


if __name__ == "__main__":
    unittest.main()
