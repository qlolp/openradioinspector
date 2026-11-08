"""Windows frequency-counter panel: honest bound, read-only on archived sweeps."""
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from radioinspector.windows.frequency_counter import (
    FrequencyCounterPanel, result_text, validate_request, validate_result,
)


def response(method="parabolic"):
    return {"session_id": "session", "stream_id": "tiny",
            "sweep": {"id": "sweep-0", "interval": {"started_at": "2026-11-08T12:00:00Z",
                                                    "ended_at": "2026-11-08T12:00:01Z"},
                      "level_unit": "dBm", "settings": {"rbw_hz": 1000}},
            "window": {"start_hz": 100, "stop_hz": 400}, "resolution_hz": 10, "raster_hz": 100,
            "method": method, "peak_index": 2, "peak_frequency_hz": 300, "peak_level_dbm": -50.0,
            "measured_frequency_hz": 343.75, "uncertainty_hz": 50.0,
            "measurement_methodology": "frequency_counter",
            "values": {"measured_frequency_hz": 343.75, "resolution_hz": 10.0, "uncertainty_hz": 50.0}}


SENT = ("sweep-0", 10, 100, 400)


def panel():
    p = FrequencyCounterPanel.__new__(FrequencyCounterPanel)
    p.app = SimpleNamespace(client=Mock(), session_id="session",
                            selected_stream_id=lambda: "tiny", root=Mock(),
                            snapshot=SimpleNamespace(session={"id": "session"},
                                                     sweeps=[{"id": "sweep-0", "level_unit": "dBm",
                                                              "stream_id": "tiny"}]))
    p.scope = (id(p.app.client), "session", "tiny")
    p.generation = 0
    p.future = p.retired = p.data = None
    p.closed = False
    p.executor = Mock(); p.executor.submit.side_effect = lambda *args: Future()
    for key in ("button", "info", "details", "start", "stop", "resolution"):
        setattr(p, key, Mock())
    p.start.get.return_value = "100"
    p.stop.get.return_value = "400"
    p.resolution.get.return_value = "10"
    return p


class CounterValidationTests(unittest.TestCase):
    def test_request_and_result_happy(self):
        self.assertEqual(validate_request(*SENT), SENT)
        self.assertEqual(validate_result(response(), "session", SENT), response())
        text = result_text(response())
        self.assertIn("343.75 Гц ± 50.0 Гц", text)
        self.assertIn("max(разрешение, сетка)/2", text)
        self.assertIn("frequency_counter", text)

    def test_request_rejects_bad_shapes(self):
        for args in (("sweep-0", 0, 100, 400), ("sweep-0", -1, 100, 400),
                     ("sweep-0", 1.5, 100, 400), ("", 10, 100, 400),
                     ("x" * 129, 10, 100, 400), ("sweep-0", 10, 400, 400),
                     ("sweep-0", 10, 500, 400), ("sweep-0", 10, -1, 400)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_request(*args)

    def test_result_rejects_forged_measurements(self):
        cases = [
            {"measured_frequency_hz": 999.0},   # outside the window
            {"uncertainty_hz": -1.0},
            {"method": "guess"},
            {"resolution_hz": 99},
            {"values": {"measured_frequency_hz": 343.75}},
        ]
        for changes in cases:
            bad = response()
            bad.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_result(bad, "session", SENT)
        bad = response(); bad["sweep"] = dict(bad["sweep"], id="other")
        with self.assertRaises(ValueError):
            validate_result(bad, "session", SENT)
        with self.assertRaises(ValueError):
            validate_result(response(), "other-session", SENT)


class CounterPanelTests(unittest.TestCase):
    def test_analyze_roundtrip_and_forged_result(self):
        p = panel()
        future = Future()
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 1)
        args = p.executor.submit.call_args[0]
        self.assertEqual(args[0], p.app.client.frequency_counter)
        self.assertEqual(args[1:], ("session",) + SENT)
        future.set_result(response())
        p.deadline = p.deadline + 100
        p.finish(p.generation)
        self.assertEqual(validate_result(p.data, "session", SENT), response())
        self.assertIn("343.75", p.info.set.call_args[0][0])
        # A forged response never freezes.
        p2 = panel()
        forged = response(); forged["measured_frequency_hz"] = 999.0
        future2 = Future(); future2.set_result(forged)
        p2.executor.submit.side_effect = lambda *args: future2
        p2.app.root.after.side_effect = lambda delay, callback: None
        p2.analyze()
        p2.deadline = p2.deadline + 100
        p2.finish(p2.generation)
        self.assertIsNone(p2.data)
        self.assertIn("недоступно", p2.info.set.call_args[0][0])

    def test_bad_inputs_never_reach_the_core(self):
        p = panel()
        p.resolution.get.return_value = "0"
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.start.get.return_value = "400"
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.stop.get.return_value = "abc"
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.app.snapshot.sweeps = []
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0, "no archived sweep refuses")

    def test_scope_change_clears_result(self):
        p = panel()
        p.data = response()
        p.show_details = Mock()
        p.app.session_id = "other"
        p.update()
        self.assertIsNone(p.data)
        self.assertEqual(p.scope, (id(p.app.client), "other", "tiny"))


if __name__ == "__main__":
    unittest.main()
