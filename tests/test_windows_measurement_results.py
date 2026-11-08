"""Windows measurement-result panel: methodology is explicit, code apart from values."""
import json
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from radioinspector.windows.measurement_results import (
    MeasurementResultPanel, result_text, validate_payload, validate_record,
)


def record(methodology="beta2_bandwidth", code="norm"):
    values = ({} if code == "not_measured"
              else {name: index + 1.0 for index, name in enumerate(
                  ["bandwidth_hz", "lower_edge_hz", "upper_edge_hz", "center_hz"])})
    return {"id": "result-1", "session_id": "session", "stream_id": "tiny",
            "methodology": methodology, "result_code": code, "frequency_hz": 101_800_000,
            "interval": {"started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"},
            "values": values, "created_at": "2026-11-08T12:00:02Z"}


def body(code="norm"):
    item = record(code=code)
    item["stream_id"] = "tiny"
    return {key: item[key] for key in ("methodology", "result_code", "frequency_hz", "interval", "values", "stream_id")}


def panel():
    p = MeasurementResultPanel.__new__(MeasurementResultPanel)
    p.app = SimpleNamespace(client=Mock(), session_id="session",
                            selected_stream_id=lambda: "tiny", root=Mock(),
                            snapshot=SimpleNamespace(session={"id": "session"}))
    p.scope = (id(p.app.client), "session")
    p.generation = 0
    p.future = p.retired = None
    p.closed = False
    p.items = []
    p.executor = Mock(); p.executor.submit.side_effect = lambda *args: Future()
    for key in ("button", "info", "details", "methodology", "code", "frequency",
                "started", "ended", "editor"):
        setattr(p, key, Mock())
    p.methodology.get.return_value = "beta2_bandwidth"
    p.code.get.return_value = "norm"
    p.frequency.get.return_value = "101800000"
    p.started.get.return_value = "2026-11-08T12:00:00Z"
    p.ended.get.return_value = "2026-11-08T12:00:01Z"
    p.editor.get.return_value = json.dumps(record()["values"])
    return p


class ResultValidationTests(unittest.TestCase):
    def test_payload_and_record_happy_paths(self):
        self.assertEqual(validate_payload(body(), "tiny"), body())
        self.assertEqual(validate_record(record(), "session", "tiny"), record())
        text = result_text([record(), record(code="not_measured")])
        self.assertIn("beta2_bandwidth", text)
        self.assertIn("not_measured", text)
        self.assertIn("код результата", text)

    def test_payload_rejects_wrong_shape_code_and_values(self):
        cases = [dict(body(), extra=1), dict(body(), stream_id="other")]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_payload(payload, "tiny")
        for code in ("ok", "norma", None, 1):
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate_payload(dict(body(), result_code=code), "tiny")
        # not_measured never carries values; norm must match the methodology exactly.
        changed = body("not_measured")
        changed["values"] = {"bandwidth_hz": 1.0}
        with self.assertRaises(ValueError):
            validate_payload(changed, "tiny")
        changed = body()
        changed["values"] = {"bandwidth_hz": 1.0}
        with self.assertRaises(ValueError):
            validate_payload(changed, "tiny")
        changed = body()
        changed["methodology"] = "control_bandwidth"
        with self.assertRaises(ValueError):
            validate_payload(changed, "tiny", )
        for field, value in (("frequency_hz", True), ("frequency_hz", 1.5),
                             ("frequency_hz", -1), ("methodology", "unknown")):
            changed = body()
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_payload(changed, "tiny")

    def test_record_rejects_foreign_session_and_nonfinite_values(self):
        changed = record()
        changed["session_id"] = "other"
        with self.assertRaises(ValueError):
            validate_record(changed, "session", "tiny")
        changed = record()
        changed["values"]["bandwidth_hz"] = float("nan")
        with self.assertRaises(ValueError):
            validate_record(changed, "session", "tiny")
        changed = record(code="not_measured")
        changed["values"] = {"bandwidth_hz": 1.0}
        with self.assertRaises(ValueError):
            validate_record(changed, "session", "tiny")


class PanelTests(unittest.TestCase):
    def test_analyze_validates_locally_and_accepts_frozen_record(self):
        p = panel()
        future = Future()
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 1)
        args = p.executor.submit.call_args[0]
        self.assertEqual(args[0], p.app.client.save_measurement_result)
        self.assertEqual(args[1], "session")
        self.assertEqual(args[2], body())
        future.set_result(record())
        p.deadline = p.deadline + 100
        p.finish(p.generation)
        self.assertEqual(len(p.items), 1)
        self.assertIn("result-1", p.info.set.call_args[0][0])
        # A forged record is refused and never freezes.
        p2 = panel()
        forged = record()
        forged["values"] = {"bandwidth_hz": 1.0}
        future2 = Future(); future2.set_result(forged)
        p2.executor.submit.side_effect = lambda *args: future2
        p2.app.root.after.side_effect = lambda delay, callback: None
        p2.analyze()
        p2.deadline = p2.deadline + 100
        p2.finish(p2.generation)
        self.assertEqual(p2.items, [])
        self.assertIn("некорректн", p2.info.set.call_args[0][0].lower())

    def test_bad_input_never_reaches_the_core(self):
        p = panel()
        p.editor.get.return_value = "{bad"
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.code.get.return_value = "not_measured"
        p.editor.get.return_value = '{"bandwidth_hz": 1}'
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0, "not_measured with values is refused")
        p = panel()
        p.frequency.get.return_value = "abc"
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)

    def test_scope_change_clears_frozen_items(self):
        p = panel()
        p.items = [record()]
        p.show_details = Mock()
        p.app.session_id = "other"
        p.update()
        self.assertEqual(p.items, [])
        self.assertEqual(p.scope, (id(p.app.client), "other"))


if __name__ == "__main__":
    unittest.main()
