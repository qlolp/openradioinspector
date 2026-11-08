"""Windows signal-list panel: export/import with unit fidelity preserved."""
import json
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from radioinspector.windows.signal_list import (
    SignalListPanel, result_text, validate_export, validate_import,
)


def entry():
    return {"frequency_hz": 101800000, "bandwidth_hz": 12500,
            "interval": {"started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"},
            "level_unit": "dBm", "peak_level": -50.5}


def artifact(count=1):
    return {"format": "openradioinspector-signal-list", "format_version": 1, "name": "Test",
            "frequency_unit": "Hz", "entries": [entry() for _ in range(count)]}


def export_response(count=1):
    return {"session_id": "session", "exported": count, "list": artifact(count)}


def import_response(count=1):
    return {"session_id": "session", "stream_id": "tiny", "imported": count,
            "candidate_ids": [f"candidate-{i}" for i in range(count)], "name": "Test"}


def panel():
    p = SignalListPanel.__new__(SignalListPanel)
    p.app = SimpleNamespace(client=Mock(), session_id="session",
                            selected_stream_id=lambda: "tiny", root=Mock(),
                            snapshot=SimpleNamespace(session={"id": "session"}))
    p.scope = (id(p.app.client), "session")
    p.generation = 0
    p.future = None
    p.closed = False
    p.executor = Mock(); p.executor.submit.side_effect = lambda *args: Future()
    for key in ("button", "import_button", "info", "details", "editor", "name"):
        setattr(p, key, Mock())
    p.name.get.return_value = "Список сигналов"
    p.editor.get.return_value = json.dumps(artifact(), ensure_ascii=False)
    return p


class SignalListValidationTests(unittest.TestCase):
    def test_export_import_and_texts(self):
        self.assertEqual(validate_export(export_response(), "session"), export_response())
        self.assertEqual(validate_import(import_response(), "session", "tiny", 1), import_response())
        self.assertIn("Экспортировано сигналов: 1", result_text("export", export_response()))
        self.assertIn("состояния new", result_text("import", import_response()))
        self.assertIn("не конвертируются", result_text("import", import_response()))

    def test_rejections(self):
        bad = export_response(); bad["session_id"] = "other"
        with self.assertRaises(ValueError):
            validate_export(bad, "session")
        bad = export_response(); bad["exported"] = 2
        with self.assertRaises(ValueError):
            validate_export(bad, "session")
        for changes in ({"imported": 2}, {"candidate_ids": ["a", "b"]}, {"stream_id": "other"},
                        {"session_id": "other"}):
            bad = import_response(); bad.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_import(bad, "session", "tiny", 1)


class SignalListPanelTests(unittest.TestCase):
    def test_export_fills_draft_and_import_roundtrip(self):
        p = panel()
        future = Future()
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.export()
        self.assertEqual(p.executor.submit.call_count, 1)
        args = p.executor.submit.call_args[0]
        self.assertEqual(args[0], p.app.client.signal_list_export)
        future.set_result(export_response())
        p.deadline = p.deadline + 100
        p.finish(p.generation, "export", "session", None, None)
        self.assertIn("Экспортировано сигналов: 1", p.details.insert.call_args[0][1])
        # Import validates the draft before sending and checks the echo.
        p2 = panel()
        future2 = Future()
        p2.executor.submit.side_effect = lambda *args: future2
        p2.app.root.after.side_effect = lambda delay, callback: None
        p2.import_list()
        args = p2.executor.submit.call_args[0]
        self.assertEqual(args[0], p2.app.client.signal_list_import)
        self.assertEqual(args[1:3], ("session", "tiny"))
        future2.set_result(import_response())
        p2.deadline = p2.deadline + 100
        p2.finish(p2.generation, "import", "session", "tiny", 1)
        self.assertIn("Импортировано сигналов: 1", p2.details.insert.call_args[0][1])

    def test_bad_draft_never_reaches_the_core(self):
        p = panel()
        p.editor.get.return_value = ""
        p.import_list()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.editor.get.return_value = "{}"
        p.import_list()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.app.client = None
        p.export()
        self.assertEqual(p.executor.submit.call_count, 0)

    def test_forged_response_never_renders(self):
        p = panel()
        future = Future()
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.export()
        future.set_result({"session_id": "other", "exported": 1, "list": artifact()})
        p.deadline = p.deadline + 100
        p.finish(p.generation, "export", "session", None, None)
        self.assertIn("недоступен", p.info.set.call_args[0][0])

    def test_scope_change_clears(self):
        p = panel()
        p.show_details = Mock()
        p.app.session_id = "other"
        p.update()
        self.assertEqual(p.scope, (id(p.app.client), "other"))


if __name__ == "__main__":
    unittest.main()
