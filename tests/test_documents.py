"""Тесты функциональности, добавленной по результатам анализа документов
«БД частотных назначений», «Планирование заданий», «InspectorDC»,
«I_MasterDevice», «Правила формирования отчетов», «Порядок анализа
результатов долговременного радиомониторинга», «I_RemoteCheck manual»,
«Геоинформационная система» и «IQ_Process».

Запуск: ``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radioinspector.geoloc import (  # noqa: E402
    antenna_compass,
    link_budget_range_km,
    radio_horizon_km,
    visibility_radius_km,
)
from radioinspector.keywords import CATALOG, KEYWORD_RE, keyword_group  # noqa: E402
from radioinspector.measurements import (  # noqa: E402
    complex_center_frequency,
    control_bandwidth,
)
from radioinspector.occupancy import (  # noqa: E402
    band_efficiency,
    duty_intervals,
    efficiency_from_store,
)
from radioinspector.panorama import (  # noqa: E402
    PanoramaStore,
    ScanSettings,
    generate_synthetic_trace,
)
from radioinspector.reporting import (  # noqa: E402
    ReportContext,
    ProtocolTemplate,
    render_template,
)
from radioinspector.tasks import (  # noqa: E402
    ACTION_CONTROL_BANDWIDTH,
    ACTION_FREQUENCY,
    ACTION_PHONOGRAM,
    ACTION_THRESHOLDS_DB,
    ControlTask,
    FrequencyControlProfile,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_DRAFT,
    STATUS_QUEUED,
    TaskBook,
)


# ---------------------------------------------------------------------------
# Словарь ключевых слов протоколов
# ---------------------------------------------------------------------------


class TestKeywordCatalog(unittest.TestCase):
    """Словарь воспроизводит схему исходного комплекса."""

    def test_catalog_contains_original_keywords(self) -> None:
        for key in (
            "TrOwner",
            "MeasDevices",
            "MOFBN_Res",
            "MOFBB2_Res",
            "MCntr_ResFc",
            "M1902_CntrMeas",
            "M1902T_Res",
            "MES_ResDBMU",
            "MER_Det1",
            "MEMOT_Fc",
            "SbRng_Busy",
            "DVBT_MER_rms",
            "EstFc_ComplexFc",
        ):
            self.assertIn(key, CATALOG, f"ключевое слово {key} отсутствует")

    def test_catalog_size(self) -> None:
        self.assertGreaterEqual(len(CATALOG), 300)

    def test_keyword_syntax_is_case_sensitive(self) -> None:
        self.assertIn("MOFBN_Res", CATALOG)
        self.assertIn("MOFBB2_Res", CATALOG)
        self.assertNotEqual(CATALOG["MOFBN_Res"], CATALOG["MOFBB2_Res"])

    def test_regex_accepts_only_latin_digits_underscore(self) -> None:
        self.assertEqual(KEYWORD_RE.findall("#TrOwner# #Meas_1#"), ["TrOwner", "Meas_1"])
        self.assertEqual(KEYWORD_RE.findall("#Порт#"), [])

    def test_keyword_group(self) -> None:
        self.assertIn("β/2", keyword_group("MOFBB2_Res"))
        self.assertIn("−N дБ", keyword_group("MOFBN_Res"))
        self.assertIn("DVB-T", keyword_group("DVBT_Modulation"))


class TestReportKeywordRendering(unittest.TestCase):
    """Подстановка по синтаксису ``#КлючевоеСлово#`` и разворачивание таблиц."""

    def test_missing_keyword_is_removed(self) -> None:
        context = ReportContext().set("TrOwner", "ООО «Альфа»")
        rendered = context.render_keywords("Владелец: #TrOwner#; адрес: #TrOwnerAddress#")
        self.assertIn("ООО «Альфа»", rendered)
        self.assertNotIn("#TrOwnerAddress#", rendered)

    def test_placeholder_syntax_still_supported(self) -> None:
        context = ReportContext().set("date", "01.01.2026")
        self.assertIn("01.01.2026", context.render("Дата: {{date}}"))

    def test_table_markers_expand_rows(self) -> None:
        template = "Шапка\n#D1#|#M1902T_Lv#|#M1902T_Meas#\n#1D#"
        tables = {
            "D1": [
                {"M1902T_Lv": "-40", "M1902T_Meas": "12"},
                {"M1902T_Lv": "-50", "M1902T_Meas": "8"},
            ]
        }
        rendered = render_template(template, ReportContext(), tables)
        self.assertIn("|-40|12", rendered)
        self.assertIn("|-50|8", rendered)
        self.assertNotIn("#D1#", rendered)

    def test_table_without_records_is_removed(self) -> None:
        template = "Шапка\n#D2#строка\n#2D#"
        rendered = render_template(template, ReportContext(), {})
        self.assertEqual(rendered.strip(), "Шапка")

    def test_template_lists_both_syntaxes(self) -> None:
        template = ProtocolTemplate("t", "{{date}} #TrOwner#")
        self.assertEqual(template.keys(), ["date", "TrOwner"])
        self.assertEqual(template.unknown_keys(), ["date"])


# ---------------------------------------------------------------------------
# Эффективность использования диапазона
# ---------------------------------------------------------------------------


def _grid_with_signal(rows: int = 10) -> np.ndarray:
    """Сетка «панорамы × частота»: сигнал присутствует в 4 из 10 измерений."""
    grid = np.full((rows, 5), -90.0)
    grid[0:4, 2] = -40.0
    return grid


class TestBandEfficiency(unittest.TestCase):
    """Метрика n/N — доля моментов наблюдения с превышением порога."""

    def test_efficiency_counts_exceedances(self) -> None:
        grid = _grid_with_signal(10)
        times = np.arange(10, dtype=float) * 30.0
        freqs = np.linspace(100e6, 100.4e6, 5)
        result = band_efficiency(grid, times, freqs, -80.0)
        self.assertEqual(result.measurements, 10)
        self.assertEqual(result.exceedances, 4)
        self.assertAlmostEqual(result.efficiency, 0.4)

    def test_interval_statistics(self) -> None:
        grid = _grid_with_signal(5)
        times = np.array([0.0, 30.0, 60.0, 90.0, 120.0])
        freqs = np.linspace(100e6, 100.4e6, 5)
        result = band_efficiency(grid, times, freqs, -80.0)
        self.assertAlmostEqual(result.mean_interval_s, 30.0)
        self.assertAlmostEqual(result.min_interval_s, 30.0)
        self.assertAlmostEqual(result.max_interval_s, 30.0)
        self.assertAlmostEqual(result.observation_time_s, 120.0)

    def test_sub_band_limits_level_search(self) -> None:
        grid = _grid_with_signal(4)
        times = np.arange(4, dtype=float)
        freqs = np.linspace(100e6, 100.4e6, 5)
        # поддиапазон без сигнала
        result = band_efficiency(grid, times, freqs, -80.0, start_hz=100.3e6, stop_hz=100.4e6)
        self.assertEqual(result.exceedances, 0)
        self.assertEqual(result.efficiency, 0.0)

    def test_duty_intervals_split_active_and_inactive(self) -> None:
        levels = np.array([-40.0, -40.0, -90.0, -40.0])
        times = np.array([0.0, 10.0, 20.0, 30.0])
        intervals = duty_intervals(levels, times, -80.0)
        self.assertEqual(len(intervals), 3)
        self.assertTrue(intervals[0].active)
        self.assertFalse(intervals[1].active)
        self.assertTrue(intervals[2].active)
        # Интервал «включено» длится до момента наблюдения выключения:
        # 0 с — включено, 20 с — выключено.
        self.assertAlmostEqual(intervals[0].duration_s, 20.0)

    def test_statistics_text(self) -> None:
        grid = _grid_with_signal(4)
        times = np.arange(4, dtype=float)
        freqs = np.linspace(100e6, 100.4e6, 5)
        result = band_efficiency(grid, times, freqs, -80.0)
        self.assertIn("вкл.", result.statistics())

    def test_statistics_matches_original_format(self) -> None:
        # Формат исходного комплекса:
        # «вкл. 19:16:06 11.04, выкл. 19:20:11 11.04, длит. 00:04:04».
        grid = _grid_with_signal(4)
        times = np.arange(4, dtype=float) * 27.646
        freqs = np.linspace(100e6, 100.4e6, 5)
        result = band_efficiency(grid, times, freqs, -80.0)
        line = result.statistics().splitlines()[0]
        self.assertRegex(
            line,
            r"^вкл\. \d{2}:\d{2}:\d{2} \d{2}\.\d{2}, "
            r"выкл\. \d{2}:\d{2}:\d{2} \d{2}\.\d{2}, "
            r"длит\. \d{2}:\d{2}:\d{2}$",
        )

    def test_statistics_lists_only_active_intervals(self) -> None:
        grid = _grid_with_signal(10)
        times = np.arange(10, dtype=float)
        freqs = np.linspace(100e6, 100.4e6, 5)
        result = band_efficiency(grid, times, freqs, -80.0)
        active = [item for item in result.intervals if item.active]
        self.assertLess(len(active), len(result.intervals))
        self.assertEqual(
            len(result.statistics().splitlines()), len(active)
        )

    def test_summary_reproduces_original_block(self) -> None:
        grid = _grid_with_signal(10)
        times = np.arange(10, dtype=float) * 27.646
        freqs = np.linspace(100e6, 100.4e6, 5)
        result = band_efficiency(grid, times, freqs, -80.0)
        summary = result.summary()
        self.assertIn(
            "Эффективность использования диапазона частот 40.0%", summary
        )
        self.assertIn("n=4", summary)
        self.assertIn("N=10", summary)
        self.assertIn("Средний интервал между измерениями 27сек646мс", summary)
        self.assertIn(
            "Статистические данные о работе источника излучения:", summary
        )

    def test_efficiency_from_store(self) -> None:
        settings = ScanSettings(
            start_hz=100e6, stop_hz=100.4e6, step_hz=100e3, points=5
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "base.pan"
            with PanoramaStore(path, settings) as store:
                trace = generate_synthetic_trace(settings, seed=1)
                for index in range(4):
                    store.append(trace, recorded_at=datetime.now(timezone.utc))
                result = efficiency_from_store(store, -80.0)
        self.assertEqual(result.measurements, 4)
        self.assertGreaterEqual(result.efficiency, 0.0)


# ---------------------------------------------------------------------------
# Контрольная полоса и комплексная оценка центральной частоты
# ---------------------------------------------------------------------------


def _signal_trace() -> "object":
    from radioinspector.trace import Trace

    freqs = np.linspace(100e6, 100.4e6, 401)
    levels = np.full(freqs.size, -100.0)
    inside = (freqs >= 100.15e6) & (freqs <= 100.25e6)
    levels[inside] = -30.0
    return Trace(freqs, levels, "dBm")


class TestControlBandwidth(unittest.TestCase):
    """Контрольная ширина полосы на уровне −30 дБ и сравнение с нормой."""

    def test_measured_control_bandwidth(self) -> None:
        result = control_bandwidth(_signal_trace())
        self.assertAlmostEqual(result.bandwidth_hz, 100e3, delta=2e3)
        self.assertEqual(result.level_db, 30.0)

    def test_verdict_norm(self) -> None:
        result = control_bandwidth(_signal_trace(), norm_hz=120e3)
        self.assertEqual(result.verdict, "норма")
        self.assertAlmostEqual(result.norm_with_tolerance_hz, 132e3)

    def test_verdict_not_norm(self) -> None:
        result = control_bandwidth(_signal_trace(), norm_hz=50e3)
        self.assertEqual(result.verdict, "не норма")

    def test_tolerance_allows_ten_percent(self) -> None:
        # норма 100 кГц, измерено 105 кГц — в пределах допуска 10 %
        result = control_bandwidth(_signal_trace(), norm_hz=100e3)
        self.assertEqual(result.verdict, "норма")

    def test_without_norm_no_verdict(self) -> None:
        result = control_bandwidth(_signal_trace())
        self.assertEqual(result.verdict, "")


class TestComplexCenterFrequency(unittest.TestCase):
    """Комплексная оценка центральной частоты."""

    def test_average_of_centroid_and_midpoint(self) -> None:
        result = complex_center_frequency(_signal_trace())
        self.assertTrue(np.isfinite(result.value_hz))
        self.assertAlmostEqual(
            result.value_hz, 0.5 * (result.centroid_hz + result.midpoint_hz)
        )

    def test_midpoint_inside_signal(self) -> None:
        result = complex_center_frequency(_signal_trace())
        self.assertAlmostEqual(result.midpoint_hz, 100.2e6, delta=3e3)


# ---------------------------------------------------------------------------
# Радиовидимость и антенный компас
# ---------------------------------------------------------------------------


class TestRadioHorizon(unittest.TestCase):
    """Модель радиовидимости R = 4,12·(√H + √h) и антенный компас."""

    def test_horizon_matches_formula(self) -> None:
        expected = 4.12 * (math.sqrt(100.0) + math.sqrt(20.0))
        self.assertAlmostEqual(radio_horizon_km(100.0, 20.0), expected, places=6)

    def test_geometric_horizon_without_refraction(self) -> None:
        expected = 3.57 * (math.sqrt(100.0) + math.sqrt(20.0))
        self.assertAlmostEqual(
            radio_horizon_km(100.0, 20.0, refraction=False), expected, places=6
        )

    def test_link_budget_range(self) -> None:
        distance = link_budget_range_km(
            100e6, transmitter_power_kw=1.0, receiver_sensitivity_dbm=-100.0
        )
        self.assertGreater(distance, 0.0)
        self.assertLess(distance, 100e3)

    def test_visibility_limited_by_horizon(self) -> None:
        radius = visibility_radius_km(
            100e6,
            transmitter_height_m=10.0,
            receiver_height_m=2.0,
            transmitter_power_kw=1.0,
            receiver_sensitivity_dbuv_per_m=20.0,
        )
        self.assertLessEqual(radius, radio_horizon_km(10.0, 2.0) + 1e-6)

    def test_antenna_compass_finds_maximum(self) -> None:
        azimuths = np.arange(0.0, 360.0, 10.0)
        levels = -70.0 - 30.0 * np.cos(np.radians(azimuths - 45.0))
        result = antenna_compass(azimuths, levels)
        self.assertAlmostEqual(result.max_azimuth_deg, 225.0, delta=15.0)
        self.assertTrue(result.null_azimuths_deg)

    def test_antenna_compass_candidate_bearings(self) -> None:
        azimuths = np.arange(0.0, 360.0, 10.0)
        levels = -100.0 + np.where(
            np.abs(((azimuths - 0.0 + 180) % 360) - 180) < 45, 20.0, 0.0
        )
        result = antenna_compass(azimuths, levels)
        bearings = result.candidate_bearings_deg()
        self.assertTrue(bearings)
        for value in bearings:
            self.assertTrue(0.0 <= value < 360.0)

    def test_antenna_compass_requires_samples(self) -> None:
        with self.assertRaises(ValueError):
            antenna_compass([0.0, 90.0], [-50.0, -60.0])


# ---------------------------------------------------------------------------
# Плановые задания
# ---------------------------------------------------------------------------


class TestControlTasks(unittest.TestCase):
    """Модель планового задания и профиль контроля фиксированных частот."""

    def test_task_requires_file_and_time(self) -> None:
        task = ControlTask(name="РКП-1")
        self.assertEqual(task.status, STATUS_DRAFT)
        self.assertFalse(task.can_queue())
        with self.assertRaises(ValueError):
            task.enqueue()

    def test_task_lifecycle(self) -> None:
        task = ControlTask(
            name="РКП-1",
            task_file="list.itskfcs",
            start_at=datetime.now(timezone.utc),
            duration_s=600,
        )
        task.enqueue()
        self.assertEqual(task.status, STATUS_QUEUED)
        task.start()
        self.assertEqual(task.status, "Выполняется")
        task.complete()
        self.assertEqual(task.status, STATUS_COMPLETED)

    def test_abort_records_outcome_kind(self) -> None:
        task = ControlTask(
            name="РКП-2",
            task_file="list.itskfcs",
            start_at=datetime.now(timezone.utc),
            duration_s=600,
        )
        task.enqueue().start().abort("принудительно завершено оператором")
        self.assertEqual(task.status, STATUS_ABORTED)
        self.assertEqual(task.outcome_kind(), "предупреждение")
        task.abort("аварийное завершение приложения")
        self.assertEqual(task.outcome_kind(), "ошибка")

    def test_profile_validates_thresholds(self) -> None:
        profile = FrequencyControlProfile(
            action=ACTION_CONTROL_BANDWIDTH, dynamic_threshold_db=10.0
        )
        messages = profile.validate()
        self.assertTrue(any("порог" in item for item in messages))

    def test_profile_validates_bandwidth_ratio(self) -> None:
        profile = FrequencyControlProfile(
            bandwidth_hz=25e3, signal_bandwidth_hz=100e3
        )
        messages = profile.validate()
        self.assertTrue(any("полоса пропускания" in item.lower() for item in messages))

    def test_profile_accepts_recommended_values(self) -> None:
        profile = FrequencyControlProfile(
            points=9,
            action=ACTION_PHONOGRAM,
            dynamic_threshold_db=ACTION_THRESHOLDS_DB[ACTION_PHONOGRAM],
            bandwidth_hz=5e3,
            signal_bandwidth_hz=100e3,
            span_hz=200e3,
            screen_fill=0.8,
        )
        self.assertEqual(profile.validate(), [])

    def test_frequency_threshold_is_higher_than_phonogram(self) -> None:
        self.assertGreater(
            ACTION_THRESHOLDS_DB[ACTION_FREQUENCY],
            ACTION_THRESHOLDS_DB[ACTION_PHONOGRAM],
        )

    def test_task_book_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.json"
            book = TaskBook(path)
            book.add(
                ControlTask(
                    name="РКП-1",
                    task_file="list.itskfcs",
                    start_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    duration_s=3600,
                )
            )
            book.save()
            loaded = TaskBook(path).load()
        self.assertEqual(len(loaded.tasks), 1)
        self.assertEqual(loaded.tasks[0].name, "РКП-1")
        self.assertIsNotNone(loaded.tasks[0].start_at)

    def test_task_book_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.json"
            book = TaskBook(path)
            book.add(
                ControlTask(
                    name="Прошедшее",
                    task_file="list.itskfcs",
                    start_at=datetime.now(timezone.utc) - timedelta(hours=1),
                    duration_s=60,
                ).enqueue()
            )
            due = book.due()
        self.assertEqual(len(due), 1)

    def test_task_book_json_is_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.json"
            TaskBook(path).save()
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["format"], "openradioinspector-task-book")


if __name__ == "__main__":
    unittest.main()
