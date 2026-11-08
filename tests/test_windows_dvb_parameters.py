"""Windows DVB-T panel: verdicts on entered numbers, never a measurement claim."""
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from radioinspector.windows.dvb_parameters import (
    DvbEvaluationPanel, result_text, validate_result,
)
from radioinspector.dvb_parameters import PARAMETER_FIELDS, THRESHOLD_FIELDS


def sent():
    return {"parameters": {"mer_db": 20.0, "ber_after_viterbi": 1e-6, "ber_after_rs": 0.0},
            "thresholds": {"min_mer_db": 15.0, "max_ber_after_viterbi": 1e-3, "max_ber_after_rs": 1e-9},
            "guard_interval": "1/4", "code_rate": "2/3"}


def response(result_code="norm", verdicts=None):
    if verdicts is None:
        verdicts = {name: result_code == "norm" for name in PARAMETER_FIELDS}
    return {"session_id": "session", "methodology": "dvb_t_parameters", "result_code": result_code,
            "guard_interval": "1/4", "code_rate": "2/3",
            "parameters": sent()["parameters"], "thresholds": sent()["thresholds"],
            "verdicts": verdicts,
            "values": {**sent()["parameters"], "guard_interval_ratio": 0.25, "code_rate_ratio": 2 / 3}}


def panel():
    p = DvbEvaluationPanel.__new__(DvbEvaluationPanel)
    p.app = SimpleNamespace(client=Mock(), session_id="session", root=Mock())
    p.scope = (id(p.app.client), "session")
    p.generation = 0
    p.future = p.retired = p.data = None
    p.closed = False
    p.executor = Mock(); p.executor.submit.side_effect = lambda *args: Future()
    for key in ("button", "info", "details", "guard", "code"):
        setattr(p, key, Mock())
    p.entries = {key: Mock() for key in (*PARAMETER_FIELDS, *THRESHOLD_FIELDS)}
    values = {"mer_db": "20", "ber_after_viterbi": "0.000001", "ber_after_rs": "0",
              "min_mer_db": "15", "max_ber_after_viterbi": "0.001", "max_ber_after_rs": "1e-9"}
    for key, raw in values.items():
        p.entries[key].get.return_value = raw
    p.guard.get.return_value = "1/4"
    p.code.get.return_value = "2/3"
    return p


class DvbPanelValidationTests(unittest.TestCase):
    def test_result_and_text(self):
        self.assertEqual(validate_result(response(), "session", sent()), response())
        text = result_text(response("not_norm", {"mer_db": False, "ber_after_viterbi": True,
                                                 "ber_after_rs": True}))
        self.assertIn("not_norm", text)
        self.assertIn("вне нормы", text)
        self.assertIn("не нормативное заключение", text)

    def test_result_rejections(self):
        cases = [{"session_id": "other"}, {"methodology": "x"}, {"result_code": "ok"},
                 {"guard_interval": "1/8"}, {"code_rate": "1/2"},
                 {"parameters": {"mer_db": 1.0, "ber_after_viterbi": 1e-6, "ber_after_rs": 0.0}},
                 {"values": {"mer_db": 1.0}}, {"verdicts": {"mer_db": 1}}]
        for changes in cases:
            bad = response()
            bad.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_result(bad, "session", sent())


class DvbPanelTests(unittest.TestCase):
    def test_analyze_roundtrip_and_forged_result(self):
        p = panel()
        future = Future()
        p.executor.submit.side_effect = lambda *args: future
        p.app.root.after.side_effect = lambda delay, callback: None
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 1)
        args = p.executor.submit.call_args[0]
        self.assertEqual(args[0], p.app.client.dvb_evaluation)
        self.assertEqual(args[2], sent())
        future.set_result(response())
        p.deadline = p.deadline + 100
        p.finish(p.generation)
        self.assertEqual(validate_result(p.data, "session", sent()), response())
        self.assertIn("norm", p.info.set.call_args[0][0])
        # A forged response never freezes.
        p2 = panel()
        forged = response()
        forged["parameters"] = {"mer_db": 99.0, "ber_after_viterbi": 1e-6, "ber_after_rs": 0.0}
        future2 = Future(); future2.set_result(forged)
        p2.executor.submit.side_effect = lambda *args: future2
        p2.app.root.after.side_effect = lambda delay, callback: None
        p2.analyze()
        p2.deadline = p2.deadline + 100
        p2.finish(p2.generation)
        self.assertIsNone(p2.data)
        self.assertIn("недоступна", p2.info.set.call_args[0][0])

    def test_bad_inputs_never_reach_the_core(self):
        p = panel()
        p.entries["mer_db"].get.return_value = "abc"
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.entries["ber_after_rs"].get.return_value = ""
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.guard.get.return_value = "1/3"
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)
        p = panel()
        p.app.client = None
        p.analyze()
        self.assertEqual(p.executor.submit.call_count, 0)

    def test_scope_change_clears_result(self):
        p = panel()
        p.data = response()
        p.show_details = Mock()
        p.app.session_id = "other"
        p.update()
        self.assertIsNone(p.data)
        self.assertEqual(p.scope, (id(p.app.client), "other"))


if __name__ == "__main__":
    unittest.main()
