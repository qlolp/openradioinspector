"""Strict UTC admission on the oldest supported Python datetime parser."""
import copy
from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from radioinspector import research_rpc as rpc


ROOT = Path(__file__).resolve().parents[1]


class Python310Datetime:
    """The Python 3.10 fromisoformat boundary: terminal Z is unsupported."""

    @staticmethod
    def fromisoformat(value):
        if value.endswith("Z"):
            raise ValueError("Invalid isoformat string")
        local = value.split("+", 1)[0]
        if "." in local and len(local.rsplit(".", 1)[1]) not in (3, 6):
            raise ValueError("Invalid isoformat string")
        return datetime.fromisoformat(value)


def documents():
    control = json.loads((ROOT / "tests/fixtures/control-science-v1.json").read_text())
    detection = json.loads((ROOT / "tests/fixtures/automatic-detection-science-v1.json").read_text())
    for case in control["cases"]:
        yield "control:" + case["method"], rpc._validate_control, {
            "version": 1, "request_id": "UTC-é-e\u0301-😀", "operation": "control_analysis",
            "method": case["method"], "source": copy.deepcopy(case["source"]), "rbw_hz": case["rbw_hz"],
        }
    for case in detection["cases"]:
        yield "detection:" + case["name"], rpc._validate_detection, {
            "version": 1, "request_id": "UTC-é-e\u0301-😀", "operation": "analysis",
            "method": "automatic_detection_regions", "sweep": copy.deepcopy(case["sweep"]),
            "threshold_dbm": case["threshold_dbm"], "maximum": case["maximum"],
        }


def interval(document):
    return document["source"] if document["operation"] == "control_analysis" else document["sweep"]["interval"]


def encoded(document):
    return json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")


class ResearchUtcPortabilityTests(unittest.TestCase):
    def test_frozen_control_and_detection_sources_accept_python310_utc_without_mutation(self):
        with patch.object(rpc, "datetime", Python310Datetime):
            for name, validate, document in documents():
                with self.subTest(case=name):
                    before = encoded(document)
                    validate(document)
                    self.assertEqual(encoded(document), before)

    def test_optional_fraction_and_equal_interval_keep_the_original_timestamp_bytes(self):
        with patch.object(rpc, "datetime", Python310Datetime):
            for name, validate, original in documents():
                stamps = ["2026-11-11T12:00:00Z"] + [
                    "2026-11-11T12:00:00." + "123456"[:precision] + "Z" for precision in range(1, 7)
                ]
                for stamp in stamps:
                    with self.subTest(case=name, stamp=stamp):
                        document = copy.deepcopy(original)
                        dates = interval(document)
                        dates["started_at"] = dates["ended_at"] = stamp
                        before = encoded(document)
                        validate(document)
                        self.assertEqual(encoded(document), before)

    def test_invalid_zone_precision_calendar_and_reversal_remain_rejected(self):
        invalid = ("2026-11-11T12:00:00+00:00", "2026-11-11T12:00:00+01:00",
                   "2026-11-11T12:00:00z", "2026-11-11T12:00:00",
                   "2026-11-11T12:00:00.1234567Z", "2026-02-30T12:00:00Z")
        with patch.object(rpc, "datetime", Python310Datetime):
            for name, validate, original in documents():
                for stamp in invalid:
                    with self.subTest(case=name, stamp=stamp):
                        document = copy.deepcopy(original)
                        interval(document)["started_at"] = stamp
                        before = encoded(document)
                        with self.assertRaises(rpc.ProtocolError):
                            validate(document)
                        self.assertEqual(encoded(document), before)
                document = copy.deepcopy(original)
                interval(document).update(started_at="2026-11-11T12:00:01Z",
                                          ended_at="2026-11-11T12:00:00Z")
                with self.subTest(case=name, reversed=True), self.assertRaisesRegex(rpc.ProtocolError, "is reversed"):
                    validate(document)

    def test_dimension_errors_still_precede_utc_calendar_validation(self):
        for name, validate, document in documents():
            source = document["source"] if document["operation"] == "control_analysis" else document["sweep"]
            source["levels"].pop()
            interval(document)["started_at"] = "2026-02-30T12:00:00Z"
            with self.subTest(case=name), patch.object(rpc, "datetime", Python310Datetime):
                with self.assertRaisesRegex(rpc.ProtocolError, "dimensions must match"):
                    validate(document)
