"""Numeric input contracts for the three session automation policies."""

import unittest

from radioinspector.automatic_completion import AutomaticInvestigationCompletion
from radioinspector.automatic_detection import AutomaticCandidateDetector
from radioinspector.automatic_handoff import AutomaticHandoffScheduler
from radioinspector.session_service import SessionService


class AutomationPolicyNumberTests(unittest.TestCase):
    def setUp(self):
        service = SessionService(store=None)
        self.addCleanup(service.close)
        self.session_id = service.create_session({"name": "Numeric policy checks"}).id
        detector = AutomaticCandidateDetector(service, start_background=False)
        handoff = AutomaticHandoffScheduler(service, tune=lambda *_: {}, start_background=False)
        completion = AutomaticInvestigationCompletion(service, start_background=False)
        self.policies = (
            (detector, "max_candidates_per_sweep", 1, 100),
            (handoff, "minimum_priority", 0, 1_000_000),
            (completion, "minimum_observations", 1, 1000),
        )
        for scheduler, *_ in self.policies:
            self.addCleanup(scheduler.close)

    def test_intervals_accept_numeric_strings_and_both_bounds(self):
        for scheduler, *_ in self.policies:
            for value, expected in (("0.1", 0.1), (" ١_٢.٥ ", 12.5), (3600, 3600.0)):
                with self.subTest(policy=type(scheduler).__name__, value=value):
                    result = scheduler.configure(self.session_id, {"interval_seconds": value})
                    interval = result["policy"]["interval_seconds"]
                    self.assertIs(type(interval), float)
                    self.assertEqual(interval, expected)

    def test_integer_bounds_are_inclusive_for_each_policy(self):
        for scheduler, field, lower, upper in self.policies:
            for value in (lower, upper):
                with self.subTest(field=field, value=value):
                    result = scheduler.configure(self.session_id, {field: value})
                    self.assertIs(type(result["policy"][field]), int)
                    self.assertEqual(result["policy"][field], value)

    def test_invalid_numeric_patches_preserve_the_policy_and_error_message(self):
        for scheduler, field, lower, upper in self.policies:
            before = scheduler.status(self.session_id)["policy"]
            finite = "interval_seconds должно быть конечным числом"
            interval_range = "interval_seconds должно быть в диапазоне 0.1…3600.0"
            integer = f"{field} должно быть целым числом"
            integer_range = f"{field} должно быть в диапазоне {lower}…{upper}"
            for key, value, error in (
                ("interval_seconds", True, finite),
                ("interval_seconds", None, finite),
                ("interval_seconds", "1__0", finite),
                ("interval_seconds", "NaN", interval_range),
                ("interval_seconds", float("inf"), interval_range),
                ("interval_seconds", 0, interval_range),
                ("interval_seconds", 3601, interval_range),
                (field, True, integer),
                (field, str(upper), integer),
                (field, float(upper), integer),
                (field, lower - 1, integer_range),
                (field, upper + 1, integer_range),
            ):
                with self.subTest(policy=type(scheduler).__name__, key=key, value=value):
                    with self.assertRaises(ValueError) as rejected:
                        scheduler.configure(self.session_id, {"enabled": True, key: value})
                    self.assertEqual(str(rejected.exception), error)
                    self.assertEqual(scheduler.status(self.session_id)["policy"], before)
