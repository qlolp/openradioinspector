"""Actual Rust HTTP/owned installed Python worker/direct-service parity.

Run explicitly after cargo build and installing the root wheel in the isolated
worker environment. Uses temporary archives and synthetic source DTOs only.
Unix verifies actual numerical results. Windows verifies explicit unavailability
without archive mutations until owned process-tree containment is implemented.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from radioinspector.contracts import Candidate, MonitoringSession, Sweep, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def field_fixture():
    return json.loads((Path(__file__).resolve().parents[3] /
                       'tests/fixtures/field-strength-rms-v2.json').read_text())


def payloads():
    mask = {"format": "openradioinspector-spectral-mask", "format_version": 1, "name": "Mask",
            "frequency_unit": "Hz", "level_unit": "dBm", "interpolation": "linear_dbm",
            "vertices": [{"frequency_hz": 0, "limit_dbm": -80}, {"frequency_hz": 1000, "limit_dbm": -80}]}
    profile = {"format": "openradioinspector-frequency-rounding", "format_version": 1, "name": "Grid",
               "frequency_unit": "Hz", "method": "nearest_grid", "tie_break": "lower",
               "rules": [{"start_frequency_hz": 0, "end_frequency_hz": 1000, "step_hz": 10,
                          "origin_frequency_hz": 0, "max_offset_hz": 1000}]}
    raster = {"format": "openradioinspector-frequency-raster", "format_version": 1, "name": "Grid",
              "frequency_unit": "Hz", "entries": [{"name": "band", "start_hz": 0,
              "stop_hz": 1000, "grid_step_hz": 10}]}
    return {
        "compare_sweeps": {"reference_id": "sweep-0", "target_id": "sweep-2"},
        "sweep_average_comparison": {"reference_ids": ["sweep-0", "sweep-1"], "target_id": "sweep-2"},
        "sweep_average_change_detection": {"reference_ids": ["sweep-0", "sweep-1"], "target_id": "sweep-2",
                                            "minimum_increase_db": 0.1, "minimum_bins": 1},
        "sweep_threshold": {"sweep_id": "sweep-1", "window_points": 3, "margin_db": 6},
        "sweep_three_thresholds": {"sweep_id": "sweep-1", "levels_dbm": [-90, -80, -70]},
        "sweep_context": {"sweep_id": "sweep-1", "before": 1, "after": 1},
        "sweep_occupancy": {"sweep_id": "sweep-1", "before": 1, "after": 1, "threshold_dbm": -80},
        "sweep_mask_analysis": {"sweep_id": "sweep-1", "mask_json": encode(mask).decode()},
        "candidate_frequency_rounding": {"candidate_id": "candidate-𐀀", "profile_json": encode(profile).decode()},
        "frequency_rounding": {"candidate_ids": ["candidate-𐀀"], "table_json": encode(raster).decode()},
        "sweep_quality": {"sweep_id": "sweep-1", "edge_margin_db": 10, "high_level_dbm": -20},
        "frequency_counter": {"sweep_id": "sweep-1", "resolution_hz": 1, "start_hz": 100, "stop_hz": 400},
        "field_strength_rms_v2": {**field_fixture()['request'], 'sweep_id': 'calibrated-source'},
        "dvb_evaluation": {"parameters": {"mer_db": 30, "ber_after_viterbi": 0.0001, "ber_after_rs": 0},
                           "thresholds": {"min_mer_db": 20, "max_ber_after_viterbi": 0.001, "max_ber_after_rs": 0},
                           "guard_interval": "1/32", "code_rate": "3/4"},
        "measurement_map": {"map_json": encode({"format": "openradioinspector-measurement-map",
            "format_version": 1, "name": "Map", "points": [{"latitude_deg": 55.75, "longitude_deg": 37.62,
            "label": "Москва", "sector": {"azimuth_deg": 90, "beamwidth_deg": 60, "radius_m": 1000}}]}).decode()},
    }


class ScientificHttpParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get("ORI_CORE_TEST_BINARY", Path(__file__).resolve().parents[1] / "target/debug/ori-core"))
        # Preserve a venv executable symlink: resolving it selects the base
        # interpreter and loses that environment's installed worker package.
        cls.worker = Path(os.path.abspath(os.environ.get("ORI_TEST_ANALYSIS_PYTHON", sys.executable)))
        if not cls.binary.is_file():
            raise RuntimeError("Build ori-core before running the scientific HTTP gate")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ori-scientific-http-")
        self.addCleanup(self.cleanup_fixture)
        data = Path(self.temp.name).resolve()
        clock = lambda: datetime(2026, 11, 11, 0, 0, 0, 123456, tzinfo=timezone.utc)
        stored = SessionService(store=SQLiteSessionStore(str(data / "sessions.sqlite3")), clock=clock)
        try:
            session = stored.create_session({"id": "session-😀", "name": "Scientific HTTP",
                "streams": [{"id": "spectrum", "device": "synthetic", "role": "spectrum_analyzer", "level_unit": "dBm"},
                            {"id": "receiver", "device": "synthetic", "role": "receiver", "level_unit": "s_meter_count"}]})
            sweeps = []
            for number in range(3):
                sweeps.append(stored.ingest_sweep({"id": f"sweep-{number}", "session_id": session.id, "stream_id": "spectrum",
                    "interval": {"started_at": f"2026-11-11T00:00:0{number}.123456Z",
                                 "ended_at": f"2026-11-11T00:00:0{number}.223456Z"},
                    "frequencies_hz": [100, 200, 300, 400], "levels": [-90.00000000000001, -80.0 + number, -60.0 + number, -90.0],
                    "level_unit": "dBm", "settings": {"rbw_hz": 10000, "extension": {"bits": [-0.0, 1.0000000000000002]}}}))
            calibrated = field_fixture()['source']
            calibrated.update(id='calibrated-source', session_id=session.id, stream_id='spectrum')
            sweeps.append(stored.ingest_sweep(calibrated))
            candidate = stored.create_candidate(session.id, {"id": "candidate-𐀀", "source_stream_id": "receiver",
                "interval": {"started_at": "2026-11-11T00:00:00.123456Z", "ended_at": "2026-11-11T00:00:00.123456Z"},
                "center_frequency_hz": 103, "level_unit": "s_meter_count", "peak_level": 137.0,
                "score": 1.0000000000000002, "observation_ids": []})
            self.reference = SessionService(store=None)
            self.reference._sessions[session.id] = MonitoringSession.from_dict(to_primitive(session))
            self.reference._sweeps = [Sweep.from_dict(to_primitive(value)) for value in sweeps]
            self.reference._candidates = {candidate.id: Candidate.from_dict(to_primitive(candidate))}
        finally:
            stored.close()
        ready = data / "ready.url"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("ORI_RUNTIME_")}
        self.child = subprocess.Popen([str(self.binary), "--mode", "replay", "--data-dir", str(data),
            "--port", "0", "--ready-file", str(ready), "--stdin-lifeline",
            "--analysis-python", str(self.worker), "--research-best-effort"],
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 5
        while not ready.exists():
            if self.child.poll() is not None:
                raise AssertionError("Owned Rust fixture exited before readiness: " + self.child.stderr.read().decode())
            if time.monotonic() >= deadline:
                raise AssertionError("Owned Rust fixture did not become ready")
            time.sleep(0.01)
        self.url = ready.read_text().strip()

    def cleanup_fixture(self):
        abnormal = False
        child = getattr(self, "child", None)
        if child is not None:
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                abnormal = True
                child.kill()  # Only this unreaped owned child, never a discovered PID.
                child.wait(timeout=5)
            abnormal = abnormal or child.returncode != 0
            child.stderr.close()
        if hasattr(self, "reference"):
            self.reference.close()
        self.temp.cleanup()
        self.assertFalse(abnormal, "Owned Rust HTTP fixture did not complete graceful shutdown")

    def request(self, method, path, body=None):
        request = Request(self.url + path, data=None if body is None else encode(body), method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            response = urlopen(request, timeout=15)
        except HTTPError as response:
            return response.code, json.loads(response.read())
        with response:
            return response.status, json.loads(response.read())

    def analyze(self, method, payload, session="session-😀"):
        routes = {"compare_sweeps": "sweep-comparison", "sweep_mask_analysis": "sweep-mask-analysis"}
        route = routes.get(method, method.replace("_", "-"))
        from urllib.parse import quote
        path = f"/api/v1/sessions/{quote(session, safe='')}/{route}"
        if method in ("compare_sweeps", "sweep_threshold"):
            return self.request("GET", path + "?" + urlencode(payload))
        return self.request("POST", path, payload)

    def direct(self, method, payload, session="session-😀"):
        selected = getattr(self.reference, method)
        if method in ("compare_sweeps", "sweep_threshold"):
            return selected(session, **payload)
        return selected(session, payload)

    def test_supported_http_methods_preserve_results_or_report_missing_worker_containment(self):
        before = self.request("GET", "/api/v1/events/history?after=0&limit=100")
        for method, payload in payloads().items():
            with self.subTest(method=method):
                expected = to_primitive(self.direct(method, copy.deepcopy(payload)))
                status, value = self.analyze(method, payload)
                if os.name == "nt":
                    self.assertEqual(status, 503, value)
                    self.assertEqual(value["error"]["code"], "research_unavailable")
                    continue
                self.assertEqual(status, 200, value)
                self.assertEqual(encode(value["data"]), encode(expected))
        self.assertEqual(self.request("GET", "/api/v1/events/history?after=0&limit=100"), before)

    def test_calibrated_field_unknown_uncertainty_and_incompatible_tract_preserve_archive(self):
        before = self.request('GET', '/api/v1/events/history?after=0&limit=100')
        payload = payloads()['field_strength_rms_v2']
        for point in payload['calibration']['points']:
            point['uncertainty_db'] = None
        status, value = self.analyze('field_strength_rms_v2', payload)
        if os.name == 'nt':
            self.assertEqual((status, value['error']['code']), (503, 'research_unavailable'))
        else:
            self.assertEqual(status, 200, value)
            self.assertIsNone(value['data']['uncertainty_db'])
            self.assertEqual(value['data']['value_dbuv_per_m'], 55.0)
            payload['calibration']['measurement_chain_id'] = 'different-chain'
            status, value = self.analyze('field_strength_rms_v2', payload)
            self.assertEqual((status, value['error']['code']), (409, 'field_strength_unavailable'))
        self.assertEqual(self.request('GET', '/api/v1/events/history?after=0&limit=100'), before)

    def test_missing_session_preserves_authoritative_validation_priority_without_a_fake_dto(self):
        if os.name == "nt":
            before = self.request("GET", "/api/v1/events/history?after=0&limit=100")
            status, response = self.analyze("dvb_evaluation", payloads()["dvb_evaluation"], session="missing-session")
            self.assertEqual(status, 503, response)
            self.assertEqual(response["error"]["code"], "research_unavailable")
            self.assertEqual(self.request("GET", "/api/v1/events/history?after=0&limit=100"), before)
            return
        for method, payload in payloads().items():
            values = (payload,) if method in ("compare_sweeps", "sweep_threshold") else (payload, {})
            for value in values:
                with self.subTest(method=method, malformed=not value):
                    with self.assertRaises(Exception) as error:
                        self.direct(method, copy.deepcopy(value), session="missing-session")
                    status, response = self.analyze(method, value, session="missing-session")
                    message = str(error.exception.args[0]) if isinstance(error.exception, KeyError) else str(error.exception)
                    self.assertEqual(status, 404 if type(error.exception).__name__ == "SessionNotFoundError" else 400)
                    self.assertEqual(response["error"]["message"], message)


if __name__ == "__main__":
    unittest.main()
