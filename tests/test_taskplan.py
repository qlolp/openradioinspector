"""Тесты модели планового задания и списка фиксированных частот.

Состав полей задания повторяет запись базы данных планирования исходного
комплекса (справочные величины вида задания, цели, предприятия, отдела,
группы, основания и носителя; служебные даты; результат; мобильность),
список фиксированных частот — второй уровень трёхуровневой модели
выполнения измерений.

Запуск: ``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radioinspector.signals import SignalList  # noqa: E402
from radioinspector.tasks import (  # noqa: E402
    ACTION_DETECT_ONLY,
    ACTION_MEASURE_SIGNAL,
    FrequencyControlProfile,
    FixedFrequency,
    FixedFrequencyList,
    ReferenceBook,
    ControlTask,
)
from radioinspector.threshold import ThresholdLine  # noqa: E402
from radioinspector.trace import Trace  # noqa: E402


class TestReferenceBook(unittest.TestCase):
    def test_identifiers_are_sequential_per_kind(self):
        book = ReferenceBook()
        first = book.add("purpose", "Контроль загрузки")
        second = book.add("purpose", "Поиск помех")
        other = book.add("department", "Отдел радиоконтроля")
        self.assertEqual(first.reference_id, 1)
        self.assertEqual(second.reference_id, 2)
        self.assertEqual(other.reference_id, 1)

    def test_name_lookup_by_identifier(self):
        book = ReferenceBook()
        item = book.add("rkp", "РКП-1")
        self.assertEqual(book.name_of("rkp", item.reference_id), "РКП-1")
        self.assertEqual(book.name_of("rkp", 999), "")
        self.assertEqual(book.name_of("rkp", None), "")

    def test_round_trip_preserves_names_and_ids(self):
        book = ReferenceBook()
        book.add("task_type", "Радиомониторинг")
        book.add("task_type", "Контроль частот")
        restored = ReferenceBook.from_dict(book.as_dict())
        self.assertEqual(
            [item.name for item in restored.list("task_type")],
            ["Радиомониторинг", "Контроль частот"],
        )
        self.assertEqual(restored.list("task_type")[1].reference_id, 2)


class TestFixedFrequencyList(unittest.TestCase):
    def test_measurement_frequencies_are_selected_by_action(self):
        task = FixedFrequencyList("Контроль 100 МГц")
        task.add(FixedFrequency(100e6, action=ACTION_MEASURE_SIGNAL))
        task.add(FixedFrequency(101e6, action=ACTION_DETECT_ONLY))
        self.assertEqual(len(task.measurement_frequencies()), 1)
        self.assertAlmostEqual(task.measurement_frequencies()[0].frequency_hz, 100e6)

    def test_empty_list_is_reported(self):
        warnings = FixedFrequencyList().validate()
        self.assertTrue(any("пуст" in message for message in warnings))

    def test_duplicate_frequency_is_reported(self):
        task = FixedFrequencyList()
        task.add(FixedFrequency(100e6))
        task.add(FixedFrequency(100e6))
        warnings = task.validate()
        self.assertTrue(any("повторно" in message for message in warnings))

    def test_unknown_action_is_reported(self):
        task = FixedFrequencyList()
        task.add(FixedFrequency(100e6, action="несуществующее"))
        warnings = task.validate()
        self.assertTrue(any("Неизвестное действие" in message for message in warnings))

    def test_frequency_lookup_with_tolerance(self):
        task = FixedFrequencyList()
        task.add(FixedFrequency(100e6))
        self.assertIsNotNone(task.find(100.001e6, tolerance_hz=10e3))
        self.assertIsNone(task.find(100.5e6, tolerance_hz=10e3))

    def test_save_and_load_round_trip(self):
        task = FixedFrequencyList("Контроль 100 МГц")
        profile = FrequencyControlProfile(points=11, dynamic_threshold_db=20.0)
        task.add(
            FixedFrequency(
                100e6,
                bandwidth_hz=200e3,
                action=ACTION_MEASURE_SIGNAL,
                profile=profile,
                comment="основной канал",
                from_assignment=True,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = task.save(Path(tmp) / "list.json")
            loaded = FixedFrequencyList.load(path)
        self.assertEqual(loaded.name, "Контроль 100 МГц")
        item = loaded.frequencies[0]
        self.assertAlmostEqual(item.frequency_hz, 100e6)
        self.assertAlmostEqual(item.bandwidth_hz, 200e3)
        self.assertEqual(item.action, ACTION_MEASURE_SIGNAL)
        self.assertEqual(item.comment, "основной канал")
        self.assertTrue(item.from_assignment)
        self.assertIsNotNone(item.profile)
        self.assertEqual(item.profile.points, 11)


class TestControlTaskPlanningFields(unittest.TestCase):
    def _task(self) -> ControlTask:
        return ControlTask(
            name="Радиомониторинг 100 МГц",
            purpose="Контроль загрузки диапазона",
            work_type="Радиомониторинг",
            operator="Иванов",
            place="г. Москва",
            start_at=datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc),
            duration_s=3600.0,
            task_type_id=1,
            purpose_id=2,
            rkp_id=3,
            department_id=4,
            group_pa_id=5,
            basis_id=6,
            carrier_id=7,
            created_at=datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc),
            prepared_at=datetime(2026, 11, 3, 19, 0, tzinfo=timezone.utc),
            real_started_at=datetime(2026, 11, 4, 9, 5, tzinfo=timezone.utc),
            real_stopped_at=datetime(2026, 11, 4, 10, 5, tzinfo=timezone.utc),
            break_at=datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc),
            break_reason="обед",
            device_names=["Rohde&Schwarz ESR3"],
            device_ids=["SCPI::10.0.0.5"],
            observers=["Петров"],
            extra_info="сектор 4",
            result_code="0",
            result_text="задание выполнено",
            process_position="позиция 12",
            mobility="выездное",
            next_times=[datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)],
            attachments=["панорама.pan"],
            protocol_file="протокол.doc",
            sleep_interval_s=60.0,
        )

    def test_round_trip_preserves_planning_fields(self):
        task = self._task()
        restored = ControlTask.from_dict(task.as_dict())
        self.assertEqual(restored.task_type_id, 1)
        self.assertEqual(restored.purpose_id, 2)
        self.assertEqual(restored.rkp_id, 3)
        self.assertEqual(restored.department_id, 4)
        self.assertEqual(restored.group_pa_id, 5)
        self.assertEqual(restored.basis_id, 6)
        self.assertEqual(restored.carrier_id, 7)
        self.assertEqual(restored.created_at, task.created_at)
        self.assertEqual(restored.prepared_at, task.prepared_at)
        self.assertEqual(restored.real_started_at, task.real_started_at)
        self.assertEqual(restored.real_stopped_at, task.real_stopped_at)
        self.assertEqual(restored.break_at, task.break_at)
        self.assertEqual(restored.break_reason, "обед")
        self.assertEqual(restored.device_names, ["Rohde&Schwarz ESR3"])
        self.assertEqual(restored.device_ids, ["SCPI::10.0.0.5"])
        self.assertEqual(restored.observers, ["Петров"])
        self.assertEqual(restored.extra_info, "сектор 4")
        self.assertEqual(restored.result_code, "0")
        self.assertEqual(restored.result_text, "задание выполнено")
        self.assertEqual(restored.process_position, "позиция 12")
        self.assertEqual(restored.mobility, "выездное")
        self.assertEqual(restored.next_times, task.next_times)
        self.assertEqual(restored.attachments, ["панорама.pan"])
        self.assertEqual(restored.protocol_file, "протокол.doc")
        self.assertAlmostEqual(restored.sleep_interval_s, 60.0)


class TestCutSpectrumDetection(unittest.TestCase):
    """Диагностика изрезанности спектра.

    Если линия порога пересекает спектр одного сигнала несколько раз, за один
    цикл сканирования регистрируется несколько узкополосных сигналов и счётчик
    обнаружений превышает число циклов сканирования.
    """

    def _trace(self, peaks, start=90e6, step=25e3, count=4000):
        freqs = start + np.arange(count) * step
        levels = np.full(count, -100.0)
        for center, width in peaks:
            mask = np.abs(freqs - center) <= width / 2.0
            levels[mask] = -20.0
        return Trace(freqs, levels)

    def test_scan_cycles_are_counted(self):
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        for _ in range(3):
            signals.update_from_trace(self._trace([(100e6, 200e3)]), line)
        self.assertEqual(signals.cycles, 3)

    def test_detection_periodicity_reports_detections_over_cycles(self):
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        for _ in range(4):
            signals.update_from_trace(self._trace([(100e6, 200e3)]), line)
        self.assertEqual(signals[0].detection_periodicity, "4/4")

    def test_single_detection_per_cycle_is_not_cut(self):
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        for _ in range(3):
            signals.update_from_trace(self._trace([(100e6, 200e3)]), line)
        self.assertFalse(signals[0].is_cut_spectrum)

    def test_wide_signal_detected_twice_in_one_cycle_is_cut(self):
        """Изрезанный спектр: широкий сигнал пересечён линией порога дважды.

        Первый цикл определяет широкий сигнал, второй цикл — тот же сигнал с
        изрезанным спектром: две узкие части попадают внутрь полосы уже
        известного сигнала и присоединяются к нему. Число обнаружений
        становится больше числа циклов сканирования.
        """
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        signals.update_from_trace(self._trace([(100e6, 400e3)]), line)
        signals.update_from_trace(
            self._trace([(100e6, 50e3), (100.1e6, 50e3)]), line
        )
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals.cycles, 2)
        self.assertEqual(signals[0].event_count, 3)
        self.assertTrue(signals[0].is_cut_spectrum)
        self.assertEqual(signals[0].detection_periodicity, "3/2")

    def test_scan_cycles_survive_save_and_load(self):
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        for _ in range(3):
            signals.update_from_trace(self._trace([(100e6, 200e3)]), line)
        with tempfile.TemporaryDirectory() as tmp:
            path = signals.save(Path(tmp) / "signals.json")
            loaded = SignalList.load(path)
        self.assertEqual(loaded.cycles, 3)
        self.assertEqual(loaded[0].cycles, 3)
        self.assertEqual(loaded[0].detection_periodicity, "3/3")
        self.assertFalse(loaded[0].is_cut_spectrum)


class TestFixedFrequencyListCommand(unittest.TestCase):
    """Командный интерфейс задания на контроль фиксированных частот."""

    def _write_list(self, directory) -> Path:
        task = FixedFrequencyList("Контроль 100 МГц")
        task.add(FixedFrequency(100e6, bandwidth_hz=200e3, action=ACTION_MEASURE_SIGNAL))
        task.add(FixedFrequency(101e6, action=ACTION_DETECT_ONLY))
        return task.save(Path(directory) / "list.json")

    def test_command_reports_list_contents(self):
        from radioinspector.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_list(tmp)
            self.assertEqual(main(["freqs", str(path), "--json"]), 0)

    def test_command_accepts_frequency_argument(self):
        from radioinspector.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_list(tmp)
            code = main(
                ["freqs", str(path), "--add", "102e6", "--action", ACTION_MEASURE_SIGNAL]
            )
            self.assertEqual(code, 0)
            loaded = FixedFrequencyList.load(path)
            self.assertEqual(len(loaded.frequencies), 3)
            self.assertAlmostEqual(loaded.frequencies[2].frequency_hz, 102e6)
            self.assertEqual(loaded.frequencies[2].action, ACTION_MEASURE_SIGNAL)

    def test_command_reports_duplicate_as_error(self):
        from radioinspector.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_list(tmp)
            code = main(["freqs", str(path), "--add", "100e6"])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
