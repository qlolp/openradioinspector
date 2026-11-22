"""Offline RF Explorer panoramas from synthetic v1 archive records."""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools.rf_explorer.export_survey import export


SCRIPT = Path(__file__).resolve().parents[1] / "tools/rf_explorer/export_survey.py"
PASS_ID = "run<&>/1"
HEADER = {"format": "openradioinspector-archive-ndjson", "format_version": 1, "session_id": "s"}
START = 15_000_000
STEP = 10_000
SVG = "{http://www.w3.org/2000/svg}"


def sweep(tile, *, total=115, points=112, detector="normal", pass_id=PASS_ID):
    """The serial adapter's complete readback shape, marked as simulated."""
    first = min((tile - 1) * points, total - points)
    frequencies = [START + (first + i) * STEP for i in range(points)]
    started = datetime(2026, 11, 22, 1, 2, 3, tzinfo=timezone.utc) + timedelta(seconds=tile - 1)
    ended = started + timedelta(milliseconds=125)
    interval = {
        key: dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        for key, dt in (("started_at", started), ("ended_at", ended))
    }
    return {
        "kind": "sweep",
        "measurement": {
            "id": f"sweep-{tile}", "session_id": "s", "stream_id": "rfe", "interval": interval,
            "frequencies_hz": frequencies, "levels": [-91.5] * points, "level_unit": "dBm",
            "settings": {
                "adapter": "rf-explorer-3g-serial", "device_mode": "spectrum_analyzer",
                "receive_profile": "rf-explorer-3g-combo-standard-1.34",
                "device_identity": {"main_model": 3, "expansion_model": 5, "firmware": "01.34"},
                "active_board": "expansion", "active_input": "right", "board_min_hz": START,
                "board_max_hz": 2_700_000_000, "board_max_span_hz": 600_000_000,
                "start_hz": frequencies[0], "stop_hz": frequencies[-1], "step_hz": STEP,
                "points": points, "rbw_hz": 15_000, "amplitude_top_dbm": -10,
                "amplitude_bottom_dbm": -120, "offset_db": 0,
                "level_encoding": "dBm = offset_db - u8 / 2", "detector": detector,
                "detector_code": 0 if detector == "normal" else 4,
                "artifact_kind": "raw_spectrum" if detector == "normal" else "processed_trace",
                "settings_source": "device_readback", "settings_readback": True,
                "timing_source": "host_frame_read", "simulated": True,
                "survey": {
                    "start_hz": START, "stop_hz": START + (total - 1) * STEP, "step_hz": STEP,
                    "pass": 1, "tile": tile, "tiles": (total + points - 1) // points,
                    "pass_id": pass_id,
                },
            },
        },
    }


class SurveyExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.source = root / "archive.ndjson"
        self.csv = root / "panorama.csv"
        self.svg = root / "panorama.svg"

    def write_archive(self, records):
        with self.source.open("w", encoding="utf-8") as output:
            print(json.dumps(HEADER), file=output)
            for item in records:
                print(json.dumps(item), file=output)

    def run_export(self, records):
        self.write_archive(records)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--input", str(self.source), "--pass-id", PASS_ID,
             "--csv", str(self.csv), "--svg", str(self.svg)],
            capture_output=True, text=True, check=False,
        )

    def test_complete_pass_keeps_both_acquisitions_at_each_overlapping_frequency(self):
        a, b = sweep(1), sweep(2)
        b["measurement"]["levels"][0] = -18.5
        foreign = sweep(1, pass_id="earlier-run")
        foreign["measurement"]["id"] = "unrelated"
        result = self.run_export([foreign, a, b])
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary["unique_points"], summary["raw_points"], summary["overlap_points"]),
                         (115, 224, 109))
        self.assertEqual((summary["wall_seconds"], summary["frame_read_seconds"],
                          summary["inter_tile_gap_seconds"]), (1.125, 0.25, 0.875))
        self.assertEqual(summary["started_at"], "2026-11-22T01:02:03.000000Z")
        self.assertEqual(summary["ended_at"], "2026-11-22T01:02:04.125000Z")
        with self.csv.open(encoding="utf-8", newline="") as data:
            rows = list(csv.DictReader(data))
        self.assertEqual([int(r["frequency_hz"]) for r in rows], list(range(START, 16_140_001, STEP)))
        row = rows[3]
        self.assertEqual((row["sweep_id"], row["point_index"], float(row["level_dbm"])),
                         ("sweep-1", "3", -91.5))
        self.assertEqual((row["overlap_sweep_id"], row["overlap_point_index"],
                          float(row["overlap_level_dbm"])), ("sweep-2", "0", -18.5))
        self.assertEqual(row["overlap_started_at"], "2026-11-22T01:02:04.000000Z")
        self.assertEqual(row["overlap_ended_at"], "2026-11-22T01:02:04.125000Z")
        self.assertEqual(rows[-1]["overlap_sweep_id"], "")
        self.assertEqual((rows[-1]["sweep_id"], rows[-1]["point_index"], rows[-1]["gap_before_tile_s"]),
                         ("sweep-2", "111", "0.875"))
        self.assertEqual((row["pass_id"], row["active_board"], row["rbw_hz"], row["detector"]),
                         (PASS_ID, "expansion", "15000", "normal"))
        figure = ET.parse(self.svg).getroot()
        labels = " ".join(figure.itertext())
        for term in ("Normal", "sequential", "MHz", "dBm", "15 kHz", "Simulated", PASS_ID):
            self.assertIn(term, labels)

    def test_partial_or_ambiguous_evidence_preserves_prior_outputs(self):
        def check(label, records, reason):
            with self.subTest(label):
                result = self.run_export(records)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason.lower(), result.stderr.lower())
                self.assertEqual(self.csv.read_bytes(), b"prior CSV")
                self.assertEqual(self.svg.read_bytes(), b"prior SVG")

        self.csv.write_bytes(b"prior CSV")
        self.svg.write_bytes(b"prior SVG")
        check("missing end", [sweep(1)], "incomplete")
        check("duplicate tile", [sweep(1), sweep(1), sweep(2)], "duplicate")
        check("wrong pass", [sweep(1, pass_id="older")], "pass")
        check("unsupported 16-point survey", [sweep(1, total=16, points=16)], "112")
        for path, value, reason in (
            (("id",), "sweep-1", "duplicate"),
            (("id",), "", "id"),
            (("stream_id",), "another instrument", "stream"),
            (("session_id",), "another session", "session"),
            (("level_unit",), "dBuV", "dBm"),
            (("settings", "points"), 113, "points"),
            (("settings", "step_hz"), 1000, "step"),
            (("settings", "start_hz"), 15_040_000, "start_hz"),
            (("settings", "stop_hz"), 16_150_000, "stop_hz"),
            (("settings", "rbw_hz"), 30_000, "rbw_hz"),
            (("settings", "active_board"), "main", "board"),
            (("settings", "detector"), "max_hold", "detector"),
            (("settings", "artifact_kind"), "processed_trace", "detector"),
            (("settings", "offset_db"), 1, "offset_db"),
            (("settings", "settings_readback"), False, "readback"),
            (("settings", "survey", "tiles"), 3, "tiles"),
            (("settings", "survey", "pass"), 2, "pass"),
            (("frequencies_hz", 2), 15_040_000, "frequency"),
            (("frequencies_hz", 2), float(15_050_000), "frequency"),
            (("levels",), [-90.0], "length"),
            (("levels", 1), float("nan"), "level"),
            (("interval", "ended_at"), "2026-11-22T01:02:03Z", "interval"),
            (("interval", "started_at"), "2026-11-22T01:02:03Z", "time"),
            (("interval", "started_at"), "2026-11-22T01:02:04", "timezone"),
        ):
            bad = sweep(2)
            container = bad["measurement"]
            for key in path[:-1]:
                container = container[key]
            container[path[-1]] = value
            check(path, [sweep(1), bad], reason)

    def test_single_window_max_hold_is_labelled_as_a_processed_trace(self):
        result = self.run_export([sweep(1, total=112, detector="max_hold")])
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary["detector"], summary["artifact_kind"], summary["overlap_points"]),
                         ("max_hold", "processed_trace", 0))
        self.assertEqual((summary["inter_tile_gap_seconds"], summary["wall_seconds"]), (0, 0.125))
        self.assertIn("Max Hold", " ".join(ET.parse(self.svg).getroot().itertext()))
        with self.csv.open(encoding="utf-8", newline="") as data:
            first = next(csv.DictReader(data))
        self.assertEqual((first.get("artifact_kind"), first.get("simulated")), ("processed_trace", "true"))

    def test_inconsistent_physical_readback_is_rejected_even_in_a_single_tile(self):
        for field, value, diagnostic in (
            ("receive_profile", "unknown", "profile"),
            ("device_identity", {"main_model": 3, "expansion_model": 5, "firmware": "99.99"}, "identity"),
            ("amplitude_bottom_dbm", -5, "amplitude"),
            ("board_max_span_hz", 900_000_000, "board"),
        ):
            with self.subTest(field):
                raw = sweep(1, total=112)
                raw["measurement"]["settings"][field] = value
                result = self.run_export([raw])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr.lower())
                self.assertFalse(self.csv.exists())
                self.assertFalse(self.svg.exists())

    def test_failure_to_publish_either_output_preserves_the_previous_pair(self):
        self.write_archive([sweep(1, total=112)])
        replace = Path.replace
        for target in (self.csv, self.svg):
            with self.subTest(target=target.name):
                self.csv.write_bytes(b"previous CSV")
                self.svg.write_bytes(b"previous SVG")

                def fail_destination(source, destination):
                    if Path(destination) == target:
                        raise PermissionError("destination is locked")
                    return replace(source, destination)

                with patch.object(Path, "replace", new=fail_destination), self.assertRaises(PermissionError):
                    export(self.source, PASS_ID, self.csv, self.svg)
                self.assertEqual(self.csv.read_bytes(), b"previous CSV")
                self.assertEqual(self.svg.read_bytes(), b"previous SVG")

    def test_overview_keeps_a_narrow_peak_at_its_measured_frequency(self):
        # More frequencies than screen columns. The peak falls between simple strided samples.
        records = [sweep(i, total=4096) for i in range(1, 38)]
        records[8]["measurement"]["levels"][81] = -20.0
        result = self.run_export(records)
        self.assertEqual(result.returncode, 0, result.stderr)
        figure = ET.parse(self.svg).getroot()
        bounds = figure.find(f".//{SVG}rect[@id='plot-area']").attrib
        trace = figure.find(f".//{SVG}polyline[@id='peak-envelope']").get("points")
        points = [tuple(map(float, pair.split(","))) for pair in trace.split()]
        self.assertLess(len(points), 4096)
        peak_x, peak_y = min(points, key=lambda pair: pair[1])
        expected_x = float(bounds["x"]) + float(bounds["width"]) * 977 / 4095
        self.assertAlmostEqual(peak_x, expected_x, places=1)
        self.assertLess(peak_y, max(y for x, y in points) - float(bounds["height"]) / 2)


if __name__ == "__main__":
    unittest.main()
