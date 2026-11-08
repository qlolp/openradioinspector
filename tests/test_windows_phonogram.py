"""Windows phonogram panel: five-second pre-history, split reported honestly."""
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from radioinspector.windows.phonogram import (
    PhonogramPanel, status_text, validate_status,
)


def state(recording=False, last=None):
    return {"session_id": "session", "stream_id": "tiny", "sample_rate_hz": 48000,
            "recording": recording, "pre_history_seconds": 5.0,
            "buffered_seconds": 2.5, "recorded_seconds": 1.25 if recording else 0.0,
            "started_at": "2026-11-08T12:00:00Z" if recording else None, "last": last}


def last():
    return {"sample_rate_hz": 48000, "pre_history_samples": 240000, "recorded_samples": 48000,
            "pre_history_seconds": 5.0, "recorded_seconds": 1.0, "duration_seconds": 6.0,
            "started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:06Z"}


def panel():
    p = PhonogramPanel.__new__(PhonogramPanel)
    p.app = SimpleNamespace(client=Mock(), session_id="session",
                            selected_stream_id=lambda: "tiny", root=Mock(),
                            snapshot=SimpleNamespace(session={"id": "session"}))
    p.scope = (id(p.app.client), "session")
    p.generation = 0
    p.future = None
    p.closed = False
    p.state = None
    p.executor = Mock(); p.executor.submit.side_effect = lambda *args: Future()
    for key in ("button", "stop_button", "info", "details"):
        setattr(p, key, Mock())
    return p


class PhonogramValidationTests(unittest.TestCase):
    def test_status_and_text(self):
        self.assertEqual(validate_status(state(True), "session", "tiny"), state(True))
        self.assertEqual(validate_status(state(False, last()), "session", "tiny"), state(False, last()))
        text = status_text(state(False, last()))
        self.assertIn("5.00 с предыстории", text)
        self.assertIn("1.00 с записи", text)
        self.assertIn("runtime-состояние", text)

    def test_status_rejections(self):
        cases = [{"session_id": "other"}, {"stream_id": "other"}, {"recording": 1},
                 {"pre_history_seconds": 4.0}, {"buffered_seconds": "x"},
                 {"last": {"pre_history_seconds": 5, "recorded_seconds": 1, "duration_seconds": 7,
                           "pre_history_samples": 1, "recorded_samples": 1, "sample_rate_hz": 48000}}]
        for changes in cases:
            bad = state(True)
            bad.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_status(bad, "session", "tiny")


class PhonogramPanelTests(unittest.TestCase):
    def test_start_stop_roundtrip_and_forged_state(self):
        p = panel()
        future = Future()
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.start()
        self.assertEqual(p.executor.submit.call_count, 1)
        args = p.executor.submit.call_args[0]
        self.assertEqual(args[0], p.app.client.phonogram_start)
        self.assertEqual(args[1:], ("session", "tiny"))
        future.set_result(state(True))
        p.deadline = p.deadline + 100
        p.finish(p.generation, "session", "tiny")
        self.assertEqual(validate_status(p.state, "session", "tiny"), state(True))
        self.assertIn("идёт", p.details.insert.call_args[0][1])
        # A forged state never freezes.
        p2 = panel()
        forged = state(True)
        forged["pre_history_seconds"] = 4.0
        future2 = Future(); future2.set_result(forged)
        p2.executor.submit.side_effect = lambda *args: future2
        p2.app.root.after.side_effect = lambda delay, callback: None
        p2.refresh()
        p2.deadline = p2.deadline + 100
        p2.finish(p2.generation, "session", "tiny")
        self.assertIsNone(p2.state)
        self.assertIn("недоступен", p2.info.set.call_args[0][0])

    def test_no_stream_never_reaches_the_core(self):
        p = panel()
        p.app.client = None
        p.start()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.app.selected_stream_id = lambda: ""
        p.stop()
        self.assertEqual(p.executor.submit.call_count, 0)

    def test_scope_change_clears_state(self):
        p = panel()
        p.state = state(True)
        p.show_details = Mock()
        p.app.session_id = "other"
        p.update()
        self.assertIsNone(p.state)
        self.assertEqual(p.scope, (id(p.app.client), "other"))


if __name__ == "__main__":
    unittest.main()
