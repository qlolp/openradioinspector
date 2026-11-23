"""Exact-bin tolerance and single-window band power on synthetic archived sweeps."""

import http.client
import json
import math
import threading
import unittest
from unittest.mock import patch

from radioinspector.contracts import to_primitive
from radioinspector.research_rpc import process_request
from radioinspector.session_service import SessionService, SweepNotFoundError
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_research_rpc import encode, request
from tests.test_rf_explorer_survey_export import START, STEP, sweep
from tests.test_sweep_comparison import create_session, measurement


class SweepResearchTests(unittest.TestCase):
    def setUp(self):
        store = SQLiteSessionStore(":memory:")
        self.service = SessionService(store=store)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)
        self.stream_id = self.session.streams[0].id
        self.tolerance = {"reference_id": "ref", "target_id": "target",
                          "upper_deviation_db": 2, "lower_deviation_db": 3}
        self.band = {"sweep_id": "target", "start_hz": START, "stop_hz": START + 3 * STEP}

    def add(self, source, name):
        raw = dict(source, session_id=self.session.id, stream_id=self.stream_id, id=name)
        return self.service.ingest_sweep(raw)

    def envelope(self, **changes):
        return self.service.sweep_tolerance_envelope(self.session.id, {**self.tolerance, **changes})

    def power(self, **changes):
        return self.service.rf_explorer_band_power(self.session.id, {**self.band, **changes})

    def test_exact_common_bins_across_sequential_survey_windows(self):
        ref = sweep(1)["measurement"]
        target = sweep(2, pass_id="next-pass")["measurement"]
        target["settings"]["survey"]["pass"] = 2
        target["levels"][:4] = [-88.5, -95.5, -89.5, -94.5]
        original_ref = self.add(ref, "ref")
        original_target = self.add(target, "target")
        original_dump = list(self.service._store._connection.iterdump())
        with patch.object(self.service._store, "list_sweeps", side_effect=AssertionError("archive scan")):
            result = self.envelope()
        self.assertEqual(result["reference"], to_primitive(original_ref))
        self.assertEqual(result["target"], to_primitive(original_target))
        self.assertEqual(result["counts"], {"above": 1, "below": 1, "within": 107, "uncovered": 3})
        self.assertEqual(result["level_unit"], "dBm")
        self.assertEqual(result["delta_unit"], "dB")
        self.assertEqual([b["classification"] for b in result["bins"][:4]], ["above", "below", "within", "within"])
        self.assertEqual(result["bins"][0], {
            "target_index": 0, "reference_index": 3, "frequency_hz": START + 3 * STEP,
            "lower_dbm": -94.5, "upper_dbm": -89.5, "delta_db": 3.0, "classification": "above",
        })
        self.assertEqual(result["bins"][-1], {
            "target_index": 111, "reference_index": None, "frequency_hz": START + 114 * STEP,
            "lower_dbm": None, "upper_dbm": None, "delta_db": None, "classification": "uncovered",
        })
        self.assertEqual(list(self.service._store._connection.iterdump()), original_dump)
        self.assertEqual(self.envelope(upper_deviation_db=0, lower_deviation_db=0)["counts"],
                         {"above": 2, "below": 2, "within": 105, "uncovered": 3})

    def test_no_reference_interpolation_even_inside_span_or_at_duplicate_frequencies(self):
        self.add(measurement(frequencies_hz=[100, 200, 200, 400], levels=[-81, -82, -83, -84]), "ref")
        self.add(measurement(frequencies_hz=[101, 200, 200, 200], levels=[-70, -80, -84, -72]), "target")
        result = self.envelope()
        self.assertEqual([b["reference_index"] for b in result["bins"]], [None, 1, 2, None])
        self.assertEqual(result["counts"], {"above": 0, "below": 0, "within": 2, "uncovered": 2})

    def test_incompatible_reference_and_invalid_parameters(self):
        self.add(sweep(1)["measurement"], "ref")
        for i, (key, value) in enumerate((("rbw_hz", 30_000), ("offset_db", 1),
                                       ("active_input", "left"), ("device_identity", {}), ("settings_readback", False),
                                       ("detector", "max_hold"), ("unknown_extension", "changed"))):
            with self.subTest(key=key):
                raw = sweep(1)["measurement"]
                raw["settings"][key] = value
                self.add(raw, str(i))
                with self.assertRaisesRegex(ValueError, "."):
                    self.envelope(target_id=str(i))
        for changes in ({"upper_deviation_db": -1}, {"lower_deviation_db": True},
                        {"lower_deviation_db": float("inf")}, {"extra": 2}, {"target_id": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.envelope(**changes)
        with self.assertRaises(SweepNotFoundError):
            self.envelope(reference_id="unknown", target_id="ref")

    def test_flat_channel_rbw_normalization_single_source_and_rpc(self):
        raw = sweep(1)["measurement"]
        target = self.add(raw, "target")
        adjacent = sweep(2)["measurement"]
        adjacent["levels"] = [-10.0] * len(adjacent["levels"])
        self.add(adjacent, "adjacent")
        original_dump = list(self.service._store._connection.iterdump())
        result = self.power()
        self.assertAlmostEqual(result["value"], -91.5 + 10 * math.log10(30_000 / 15_000))
        self.assertEqual((result["unit"], result["rbw_hz"], result["start_index"], result["stop_index"]),
                         ("dBm", 15_000, 0, 3))
        self.assertEqual((result["start_hz"], result["stop_hz"]), (START, START + 3 * STEP))
        self.assertEqual(result["source"], to_primitive(target))
        self.assertEqual(result["integration"], "trapezoid_over_actual_grid")
        self.assertEqual(result["enbw_correction"], "not_calibrated")
        self.assertIsNone(result["uncertainty_db"])
        self.assertEqual(result["hardware_validation"], "not_run")
        rpc = request("rf_explorer_band_power", self.band)
        rpc.update(session=to_primitive(self.session), sweeps=[to_primitive(target)], candidates=[])
        reply = process_request(encode(rpc))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], result)
        self.assertEqual(list(self.service._store._connection.iterdump()), original_dump)

    def test_linear_power_not_average_dbm_and_single_missing_rbw(self):
        raw = sweep(1)["measurement"]
        raw["levels"][:3] = [-60, -50, -60]
        self.add(raw, "target")
        estimated = self.power(stop_hz=START + 2 * STEP)["value"]
        self.assertAlmostEqual(estimated, 10 * math.log10((1e-6 + 1e-5) * STEP / 15_000))
        raw["settings"]["rbw_hz"] = None
        self.add(raw, "unknown-rbw")
        with self.assertRaises(ValueError):
            self.power(sweep_id="unknown-rbw")

    def test_python_http_routes_originals_conflicts_and_query_body_limits(self):
        self.add(sweep(1)["measurement"], "target")
        self.add(sweep(2)["measurement"], "ref")
        server = create_server(host="127.0.0.1", port=0, sessions=self.service)
        runner = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        runner.start()
        try:
            def post(path, payload):
                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                try:
                    connection.request("POST", f"/api/v1/sessions/{self.session.id}/" + path,
                                       json.dumps(payload), {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            for route, body, expected in (
                    ("rf-explorer-band-power", self.band, self.power()),
                    ("sweep-tolerance-envelope", self.tolerance, self.envelope())):
                status, reply = post(route, body)
                self.assertEqual(status, 200, reply)
                self.assertEqual(reply["data"], expected)
                self.assertEqual(post(route + "?extra=1", body)[0], 400)
                self.assertEqual(post(route, {"ignored": "x" * 8192})[0], 413)
            status, invalid = post("rf-explorer-band-power", dict(self.band, stop_hz=START + 112 * STEP))
            self.assertEqual((status, invalid["error"]["code"]), (409, "band_power_unavailable"))
            status, missing = post("sweep-tolerance-envelope", dict(self.tolerance, reference_id="foreign"))
            self.assertEqual((status, missing["error"]["code"]), (404, "sweep_not_found"))
        finally:
            server.shutdown()
            runner.join(3)
            server.server_close()

    def test_rejects_partial_band_missing_points_held_trace_and_unconfirmed_settings(self):
        self.add(sweep(1)["measurement"], "target")
        self.add(sweep(2)["measurement"], "adjacent")
        for changes in ({"start_hz": START - STEP}, {"stop_hz": START + 112 * STEP},
                        {"start_hz": START + 1}, {"start_hz": True}, {"stop_hz": START},
                        {"rbw_hz": 15000}, {"sweep_ids": ["target", "adjacent"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.power(**changes)
        for i, (key, value) in enumerate((("settings_readback", False), ("rbw_hz", 0),
                                         ("rbw_hz", 3000), ("settings_source", "requested"),
                                         ("detector", "max_hold"), ("artifact_kind", "processed_trace"),
                                         ("step_hz", 15000), ("points", 111), ("adapter", "tinysa"))):
            with self.subTest(key=key, value=value):
                raw = sweep(1)["measurement"]
                raw["settings"][key] = value
                self.add(raw, str(i))
                with self.assertRaises(ValueError):
                    self.power(sweep_id=str(i))
        for value in (0, 1):
            raw = sweep(1)["measurement"]
            raw["frequencies_hz"][5] = raw["frequencies_hz"][4] + value
            self.add(raw, f"gap-{value}")
            with self.assertRaises(ValueError):
                self.power(sweep_id=f"gap-{value}")


if __name__ == "__main__":
    unittest.main()
