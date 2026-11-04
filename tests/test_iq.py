"""Тесты расчёта требований к IQ-данным.

Алгоритм исходного комплекса: для анализа цифровых сигналов выбирается
максимальная требуемая частота дискретизации Fd и максимальная требуемая
длительность записи L, проверяется наличие памяти M = Fd · L; при
недостатке памяти длительность ограничивается величиной L1 = M_доступное / Fd,
а стандарты, требующие большей длительности, не анализируются.

Запуск: ``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radioinspector.iq import (  # noqa: E402
    IQ_REQUIREMENTS,
    IqRequirement,
    plan_iq_analysis,
)


class TestIqCatalogue(unittest.TestCase):
    def test_catalogue_covers_documented_standards(self):
        names = {item.standard for item in IQ_REQUIREMENTS}
        for standard in ("DECT", "GSM", "TETRA", "APCO25", "DMR", "AnalogTV"):
            self.assertIn(standard, names)

    def test_documented_durations_are_preserved(self):
        by_name = {item.standard: item for item in IQ_REQUIREMENTS}
        self.assertAlmostEqual(by_name["DECT"].min_duration_s, 0.2)
        self.assertAlmostEqual(by_name["GSM"].min_duration_s, 0.98)
        self.assertAlmostEqual(by_name["TETRA"].min_duration_s, 1.076)
        self.assertAlmostEqual(by_name["APCO25"].min_duration_s, 0.8)
        self.assertAlmostEqual(by_name["DMR"].min_duration_s, 0.8)
        self.assertAlmostEqual(by_name["AnalogTV"].min_duration_s, 0.1)

    def test_documented_sampling_rate_of_dect(self):
        by_name = {item.standard: item for item in IQ_REQUIREMENTS}
        self.assertAlmostEqual(by_name["DECT"].min_sample_rate_hz, 2_000_000.0)


class TestIqPlan(unittest.TestCase):
    def _standards(self):
        names = ("TETRA", "APCO25", "DMR", "GSM", "Bluetooth", "DECT")
        return [item for item in IQ_REQUIREMENTS if item.standard in names]

    def test_sufficient_memory_allows_all_standards(self):
        plan = plan_iq_analysis(
            self._standards(), sample_rate_hz=2_000_000.0, memory_words=4_000_000
        )
        self.assertAlmostEqual(plan.required_rate_hz, 2_000_000.0)
        self.assertAlmostEqual(plan.required_duration_s, 1.076)
        self.assertAlmostEqual(plan.required_words, 2_152_000.0)
        self.assertAlmostEqual(plan.allowed_duration_s, 1.076)
        self.assertTrue(all(item.allowed for item in plan.standards))

    def test_insufficient_memory_limits_duration(self):
        """Пример из документации: Rohde&Schwarz FSU, 523 776 слов IQ."""
        plan = plan_iq_analysis(
            self._standards(), sample_rate_hz=2_000_000.0, memory_words=523_776
        )
        self.assertAlmostEqual(plan.allowed_duration_s, 0.261888, places=6)
        allowed = {item.standard for item in plan.standards if item.allowed}
        self.assertEqual(allowed, {"DECT"})

    def test_insufficient_sample_rate_is_reported(self):
        plan = plan_iq_analysis(
            self._standards(), sample_rate_hz=1_000_000.0, memory_words=4_000_000
        )
        self.assertFalse(plan.possible)
        self.assertTrue(
            all("частот" in item.reason for item in plan.standards)
        )

    def test_empty_requirements_are_rejected(self):
        plan = plan_iq_analysis([], sample_rate_hz=2_000_000.0, memory_words=1_000)
        self.assertFalse(plan.possible)
        self.assertIn("не заданы", plan.reason)

    def test_custom_requirement_is_accepted(self):
        plan = plan_iq_analysis(
            [IqRequirement("Custom", 4_000_000.0, 0.5)],
            sample_rate_hz=8_000_000.0,
            memory_words=8_000_000,
        )
        self.assertTrue(plan.possible)
        self.assertAlmostEqual(plan.required_rate_hz, 4_000_000.0)
        self.assertTrue(plan.standards[0].allowed)


class TestIqCommand(unittest.TestCase):
    """Командный интерфейс расчёта требований к IQ."""

    def test_command_reports_plan(self):
        from radioinspector.cli import main

        code = main(
            [
                "iq",
                "--standards",
                "DECT,GSM,TETRA",
                "--rate",
                "2000000",
                "--memory",
                "523776",
            ]
        )
        self.assertEqual(code, 0)

    def test_command_reports_missing_requirements(self):
        from radioinspector.cli import main

        code = main(["iq", "--standards", "", "--rate", "2000000", "--memory", "1000"])
        self.assertEqual(code, 1)

    def test_command_json_output(self):
        from radioinspector.cli import main

        code = main(
            ["iq", "--standards", "DECT", "--rate", "2000000", "--memory", "1000000", "--json"]
        )
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
