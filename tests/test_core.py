"""Тесты ядра OpenRadioInspector.

Запуск: ``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radioinspector.measurements import (  # noqa: E402
    bandwidth_at_level,
    center_frequency_centroid,
    field_strength,
    measure_signal,
    occupied_bandwidth_beta2,
)
from radioinspector.panorama import (  # noqa: E402
    PanoramaLibrary,
    PanoramaStore,
    ScanSettings,
    generate_synthetic_trace,
)
from radioinspector.signals import SignalList  # noqa: E402
from radioinspector.threshold import (  # noqa: E402
    ThresholdLine,
    exceeded_intervals,
    power_over_threshold,
)
from radioinspector.trace import Trace  # noqa: E402
from radioinspector.units import convert, normalize_unit  # noqa: E402


def make_trace(freqs, levels, unit="dBm") -> Trace:
    return Trace(np.asarray(freqs, dtype=float), np.asarray(levels, dtype=float), unit)


class TestUnits(unittest.TestCase):
    def test_power_roundtrip(self):
        self.assertAlmostEqual(convert(30.0, "dBm", "mW"), 1000.0, places=6)
        self.assertAlmostEqual(convert(1000.0, "mW", "dBm"), 30.0, places=9)
        self.assertAlmostEqual(convert(0.0, "dBW", "W"), 1.0, places=9)

    def test_voltage_power_with_impedance(self):
        # 1 В на 50 Ом = 20 мВт = 13 дБм.
        value = convert(1.0, "V", "dBm", impedance=50.0)
        self.assertAlmostEqual(value, 13.0103, places=3)

    def test_voltage_db(self):
        # Увеличение напряжения в 10 раз соответствует +20 дБ.
        self.assertAlmostEqual(convert(20.0, "dBuV", "uV"), 10.0, places=6)
        self.assertAlmostEqual(convert(40.0, "dBuV", "uV"), 100.0, places=6)
        self.assertAlmostEqual(convert(0.0, "dBmV", "V"), .001, places=9)

    def test_cyrillic_aliases(self):
        self.assertEqual(normalize_unit("дБмкВ"), "dBuV")
        self.assertEqual(normalize_unit("дБм"), "dBm")
        self.assertEqual(normalize_unit("дБВт"), "dBW")

    def test_field_conversion(self):
        # 0 дБмкВ/м при 50 Ом даёт −107 дБм на входе приёмника.
        value = convert(0.0, "dBuV/m", "dBm", impedance=50.0)
        self.assertAlmostEqual(value, -107.0, delta=0.05)


class TestTrace(unittest.TestCase):
    def test_step_and_span(self):
        trace = make_trace([100e6, 100.1e6, 100.2e6], [-10, -20, -30])
        self.assertAlmostEqual(trace.step, 100e3)
        self.assertAlmostEqual(trace.span, 200e3)
        self.assertAlmostEqual(trace.peak, -10.0)

    def test_nearest_index_snapping(self):
        trace = make_trace([100e6, 101e6, 102e6], [-10, -20, -30])
        self.assertEqual(trace.nearest_index(100.9e6), 1)
        self.assertEqual(trace.value_at(100.9e6), -20.0)

    def test_decimate_uses_bucket_maximum(self):
        # 500 точек, отображение в 100 — в каждой корзине выбирается максимум.
        freqs = np.linspace(0, 500, 500, endpoint=False)
        levels = np.arange(500, dtype=float)
        trace = Trace(freqs, levels)
        reduced = trace.decimate(100)
        self.assertEqual(reduced.size, 100)
        self.assertAlmostEqual(reduced.levels[0], 4.0)
        self.assertAlmostEqual(reduced.levels[1], 9.0)

    def test_decimate_noop_when_fewer_points(self):
        trace = make_trace([1, 2, 3], [-1, -2, -3])
        self.assertEqual(trace.decimate(10).size, 3)

    def test_slice_uses_measurement_points(self):
        trace = make_trace([1, 2, 3, 4, 5], [-1, -2, -3, -4, -5])
        self.assertEqual(trace.slice(1.5, 3.5).size, 3)


class TestOccupiedBandwidthBeta2(unittest.TestCase):
    def test_flat_top_signal(self):
        # Прямоугольный сигнал 100 кГц с постоянной мощностью на плато.
        freqs = np.arange(0, 1001) * 1000.0
        levels = np.full_like(freqs, -50.0)
        levels[400:601] = -20.0
        trace = Trace(freqs, levels)

        result = occupied_bandwidth_beta2(trace, beta=1.0)
        # Плато занимает 200 кГц, но границы β/2 при β=1% (по 0,5 % с каждой
        # стороны) отсекают часть плато; ожидаем порядка 195 кГц.
        self.assertGreater(result.bandwidth_hz, 150e3)
        self.assertLess(result.bandwidth_hz, 205e3)
        self.assertAlmostEqual(result.center_hz, 500.0e3, delta=1e6)

    def test_result_is_symmetric_for_symmetric_signal(self):
        freqs = np.arange(-100, 101) * 1000.0
        levels = -60.0 - 0.001 * freqs**2 / 1e6
        levels = np.where(np.abs(freqs) <= 40e3, -20.0, levels)
        trace = Trace(freqs, levels)
        result = occupied_bandwidth_beta2(trace, beta=1.0)
        self.assertAlmostEqual(
            result.lower_edge_hz + result.upper_edge_hz, 0.0, delta=500.0
        )

    def test_excludes_exactly_beta_percent_of_power(self):
        """Вне измеренной полосы остаётся ровно β % мощности сигнала."""
        freqs = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        levels = np.array([-1000.0, 0.0, 0.0, 0.0, -1000.0])
        trace = Trace(freqs, levels)
        result = occupied_bandwidth_beta2(trace, beta=1.0)

        step = 1.0
        power = np.power(10.0, levels / 10.0)
        total = float(power.sum())

        def _integrated(a: float, b: float) -> float:
            """Мощность в интервале при равномерном распределении по ячейкам."""
            total_p = 0.0
            for f, p in zip(freqs, power):
                lo = max(a, f - step / 2.0)
                hi = min(b, f + step / 2.0)
                if hi > lo:
                    total_p += p * (hi - lo) / step
            return total_p

        inside = _integrated(result.lower_edge_hz, result.upper_edge_hz)
        self.assertAlmostEqual(inside / total, 0.99, places=6)

    def test_isolated_line_bandwidth_matches_step(self):
        """Одиночная линия даёт полосу, сравнимую с шагом перестройки."""
        freqs = np.arange(0, 21) * 1000.0
        levels = np.full(21, -120.0)
        levels[10] = -20.0
        trace = Trace(freqs, levels)
        result = occupied_bandwidth_beta2(trace, beta=1.0)
        self.assertAlmostEqual(result.bandwidth_hz, 1000.0, delta=120.0)

    def test_rejects_bad_beta(self):
        trace = make_trace([1, 2], [-10, -10])
        with self.assertRaises(ValueError):
            occupied_bandwidth_beta2(trace, beta=0)


class TestBandwidthAtLevel(unittest.TestCase):
    def test_gaussian_at_minus_6db(self):
        # Гауссов импульс: ширина на уровне −6 дБ.
        # Уровень задан как −20 − 20·log10(2)·(f/σ)², поэтому уровень падает
        # на 6 дБ при |f| = σ·√(6 / (20·log10 2)).
        sigma = 10.0
        freqs = np.linspace(-60, 60, 2401)
        levels = -20.0 - 20.0 * np.log10(2.0) * (freqs / sigma) ** 2
        trace = Trace(freqs, levels)

        result = bandwidth_at_level(trace, n_db=6.0)
        expected = 2.0 * sigma * math.sqrt(6.0 / (20.0 * math.log10(2.0)))
        self.assertAlmostEqual(result.bandwidth_hz, expected, delta=0.5)

    def test_signal_inside_view_has_own_bandwidth(self):
        # Сигнал в середине полосы обзора: границы полосы должны
        # определяться переходом уровня, а не краями обзора.
        freqs = np.linspace(100e6, 100.4e6, 401)
        levels = np.full(freqs.size, -100.0)
        levels[(freqs >= 100.15e6) & (freqs <= 100.25e6)] = -30.0
        trace = Trace(freqs, levels)

        result = bandwidth_at_level(trace, n_db=30.0)
        self.assertAlmostEqual(result.bandwidth_hz, 100e3, delta=2e3)
        self.assertLess(result.bandwidth_hz, trace.span)

    def test_flat_signal_spans_view(self):
        freqs = np.arange(0, 101) * 1000.0
        levels = np.full_like(freqs, -30.0)
        trace = Trace(freqs, levels)
        result = bandwidth_at_level(trace, n_db=10.0)
        self.assertAlmostEqual(result.bandwidth_hz, 100e3, delta=1.0)

    def test_sloped_edge_is_interpolated(self):
        # Линейный спад уровня от −30 до −60 дБ: переход −60 дБ находится
        # ровно в середине спада.
        freqs = np.linspace(0.0, 1e6, 1001)
        levels = np.full(freqs.size, -100.0)
        levels[400:601] = np.linspace(-30.0, -60.0, 201)
        trace = Trace(freqs, levels)

        result = bandwidth_at_level(trace, n_db=30.0)
        self.assertAlmostEqual(result.bandwidth_hz, 200e3, delta=2e3)

    def test_rejects_non_positive_level(self):
        trace = make_trace([1, 2], [-10, -10])
        with self.assertRaises(ValueError):
            bandwidth_at_level(trace, n_db=0)


class TestFieldStrength(unittest.TestCase):
    def test_median_over_panoramas(self):
        traces = [
            make_trace([1, 2], [-50.0, -50.0]),
            make_trace([1, 2], [-40.0, -40.0]),
            make_trace([1, 2], [-60.0, -60.0]),
        ]
        result = field_strength(traces, method="median")
        self.assertAlmostEqual(result.value, -50.0)

    def test_mean_and_max(self):
        traces = [
            make_trace([1, 2], [-50.0, -50.0]),
            make_trace([1, 2], [-40.0, -40.0]),
        ]
        self.assertAlmostEqual(field_strength(traces, method="mean").value, -45.0)
        self.assertAlmostEqual(field_strength(traces, method="max").value, -40.0)

    def test_calibration_offset_added(self):
        result = field_strength(make_trace([1, 2], [-50.0, -50.0]), calibration_db=3.0)
        self.assertAlmostEqual(result.value, -47.0)


class TestCenterFrequencyCentroid(unittest.TestCase):
    def test_symmetric_signal(self):
        # Симметричный сигнал на 100 МГц в шуме.
        freqs = np.linspace(80e6, 120e6, 2001)
        levels = np.full_like(freqs, -80.0)
        mask = np.abs(freqs - 100e6) <= 500e3
        levels[mask] = -20.0
        trace = Trace(freqs, levels)

        result = center_frequency_centroid(trace)
        self.assertTrue(math.isfinite(result.value))
        self.assertAlmostEqual(result.value, 100e6, delta=50e3)

    def test_reports_low_point_count(self):
        freqs = np.linspace(99e6, 101e6, 51)
        levels = np.full_like(freqs, -80.0)
        levels[20:31] = -20.0
        result = center_frequency_centroid(Trace(freqs, levels))
        self.assertTrue(any("точек" in w for w in result.warnings))


class TestThreshold(unittest.TestCase):
    def test_default_line_is_two_thirds_of_grid(self):
        trace = make_trace([1, 2, 3], [-100.0, -50.0, 0.0])
        line = ThresholdLine.default_for(trace)
        self.assertAlmostEqual(line.levels[0], -100.0 + (2.0 / 3.0) * 100.0)

    def test_exceeded_intervals(self):
        freqs = np.arange(0, 11) * 1000.0
        levels = np.array([-100] * 3 + [-20] * 3 + [-100] * 5, dtype=float)
        trace = Trace(freqs, levels)
        line = ThresholdLine.horizontal(0.0, 10000.0, -50.0)
        intervals = exceeded_intervals(trace, line)
        self.assertEqual(len(intervals), 1)
        self.assertAlmostEqual(intervals[0][0], 3000.0)
        self.assertAlmostEqual(intervals[0][1], 5000.0)

    def test_power_over_threshold(self):
        freqs = np.arange(0, 11) * 1000.0
        levels = np.array([-100] * 3 + [-20] * 3 + [-100] * 5, dtype=float)
        trace = Trace(freqs, levels)
        line = ThresholdLine.horizontal(0.0, 10000.0, -50.0)
        power = power_over_threshold(trace, line, (3000.0, 5000.0))
        # Три точки на 20 дБ над порогом −50 дБ: 3 · (0,01 − 0,00001) мВт.
        self.assertAlmostEqual(power, 3 * (0.01 - 1e-5) / 1000.0, places=9)

    def test_save_and_load(self):
        import tempfile

        line = ThresholdLine.horizontal(0.0, 1e6, -80.0, index=1)
        with tempfile.TemporaryDirectory() as tmp:
            path = line.save(Path(tmp) / "line.thr")
            loaded = ThresholdLine.load(path)
        self.assertEqual(loaded.index, 1)
        self.assertAlmostEqual(loaded.levels[0], -80.0)


class TestSignalList(unittest.TestCase):
    def _trace_with_signal(self, center_hz, width_hz, level=-20.0, start=90e6, step=100e3):
        count = 1000
        freqs = start + np.arange(count) * step
        levels = np.full(count, -100.0)
        mask = np.abs(freqs - center_hz) <= width_hz / 2.0
        levels[mask] = level
        return Trace(freqs, levels)

    def test_new_signal_is_added(self):
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        signals.update_from_trace(self._trace_with_signal(100e6, 200e3), line)
        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0].frequency_hz, 100e6, delta=200e3)
        self.assertAlmostEqual(signals[0].bandwidth_hz, 200e3, delta=5000)

    def test_repeat_detection_updates_same_record(self):
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        for _ in range(3):
            signals.update_from_trace(self._trace_with_signal(100e6, 200e3), line)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].event_count, 3)

    def test_wider_bandwidth_creates_new_record(self):
        """Полоса более чем в 5 раз шире образует новый сигнал."""
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        signals.update_from_trace(self._trace_with_signal(100e6, 200e3), line)
        signals.update_from_trace(self._trace_with_signal(100e6, 2e6), line)
        self.assertEqual(len(signals), 2)

    def test_fixed_signal_is_not_recomputed(self):
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        signals.update_from_trace(self._trace_with_signal(100e6, 200e3), line)
        signals.edit_signal(0, 111e6, 300e3)
        signals.update_from_trace(self._trace_with_signal(100e6, 200e3), line)
        self.assertAlmostEqual(signals[0].frequency_hz, 111e6)

    def test_assignment_highlighting(self):
        from radioinspector.assignments import Assignment

        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        signals.update_from_trace(self._trace_with_signal(100e6, 200e3), line)

        assignment = Assignment(
            id=1, name="Базовая станция", tx_frequency_hz=100e6, bandwidth_hz=200e3
        )
        matched = signals.highlight_assignments([assignment])
        self.assertEqual(len(matched), 1)
        self.assertTrue(signals[0].from_assignment)

    def test_power_threshold_suppresses_weak_events(self):
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        # Сигнал лишь на 1 дБ выше порога даёт малую избыточную мощность.
        weak = self._trace_with_signal(100e6, 200e3, level=-49.0)
        signals.update_from_trace(weak, line, power_threshold_w=1e-6)
        self.assertEqual(len(signals), 0)

    def test_power_threshold_keeps_strong_events(self):
        signals = SignalList()
        line = ThresholdLine.horizontal(90e6, 190e6, -50.0)
        strong = self._trace_with_signal(100e6, 200e3, level=-20.0)
        signals.update_from_trace(strong, line, power_threshold_w=1e-6)
        self.assertEqual(len(signals), 1)


class TestPanoramaStore(unittest.TestCase):
    def _settings(self) -> ScanSettings:
        return ScanSettings(
            device="FSH3",
            antenna="ANT1",
            tract="TRACT1",
            start_hz=100e6,
            stop_hz=101e6,
            bandwidth_hz=100e3,
            step_hz=1000.0,
            points=101,
            detector="MaxPeak",
            reference_level_db=0.0,
            video_bandwidth_hz=100e3,
        )

    def test_filename_contains_scan_parameters(self):
        name = self._settings().filename()
        self.assertIn("DevFSH3", name)
        self.assertIn("From100.0", name)
        self.assertIn("To101.0", name)
        self.assertIn("Points101", name)
        self.assertTrue(name.endswith(".pan"))

    def test_direct_mode_filename(self):
        settings = self._settings()
        settings.mode = "direct"
        settings.centre_hz = 100.5e6
        settings.span_hz = 1e6
        name = settings.filename()
        self.assertIn("Cnt100.5", name)
        self.assertIn("Span1.0", name)

    def test_append_and_read(self):
        import tempfile

        settings = self._settings()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.pan"
            with PanoramaStore(path, settings) as store:
                trace = generate_synthetic_trace(settings, seed=1)
                store.append(trace, latitude=55.0, longitude=37.0)
                self.assertEqual(store.count, 1)
                loaded = store.latest()
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.trace.size, trace.size)
                self.assertAlmostEqual(loaded.latitude, 55.0)
                self.assertAlmostEqual(
                    float(loaded.trace.levels[10]), float(trace.levels[10]), places=6
                )

    def test_aggregate_traces(self):
        import tempfile

        settings = self._settings()
        with tempfile.TemporaryDirectory() as tmp:
            with PanoramaStore(Path(tmp) / "a.pan", settings) as store:
                t1 = make_trace(np.arange(10) * 1000.0, np.full(10, -50.0))
                t2 = make_trace(np.arange(10) * 1000.0, np.full(10, -30.0))
                store.append(t1)
                store.append(t2)
                self.assertAlmostEqual(store.max_trace().levels[0], -30.0)
                self.assertAlmostEqual(store.min_trace().levels[0], -50.0)
                self.assertAlmostEqual(store.average_trace().levels[0], -40.0)

    def test_library_continues_existing_store(self):
        import tempfile

        settings = self._settings()
        with tempfile.TemporaryDirectory() as tmp:
            library = PanoramaLibrary(tmp)
            store, existed = library.open_for(settings)
            self.assertFalse(existed)
            store.append(make_trace(np.arange(5) * 1000.0, np.full(5, -40.0)))
            store.close()

            store2, existed2 = library.open_for(settings)
            self.assertTrue(existed2)
            store2.append(make_trace(np.arange(5) * 1000.0, np.full(5, -40.0)))
            self.assertEqual(store2.count, 2)
            store2.close()

    def test_threshold_persistence(self):
        import tempfile

        settings = self._settings()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.pan"
            with PanoramaStore(path, settings) as store:
                line = ThresholdLine.horizontal(0.0, 1e6, -77.0)
                store.save_threshold(line)
            with PanoramaStore(path) as store:
                loaded = store.load_threshold(0)
                self.assertIsNotNone(loaded)
                self.assertAlmostEqual(loaded.levels[0], -77.0)

    def test_select_by_time(self):
        import tempfile
        from datetime import datetime, timedelta, timezone

        settings = self._settings()
        with tempfile.TemporaryDirectory() as tmp:
            with PanoramaStore(Path(tmp) / "c.pan", settings) as store:
                base = datetime.now(timezone.utc)
                for i in range(5):
                    store.append(
                        make_trace(np.arange(5) * 1000.0, np.full(5, -40.0)),
                        recorded_at=base + timedelta(seconds=i),
                    )
                subset = store.select(since=base + timedelta(seconds=2))
                self.assertEqual(len(subset), 3)


class TestAssignmentDatabase(unittest.TestCase):
    def test_add_and_query(self):
        from radioinspector.assignments import Assignment, AssignmentDatabase

        with AssignmentDatabase() as db:
            owner_id = db.add_owner("Оператор связи", inn="7701234567")
            band_id = db.add_band_type("GSM 900", tx_start_hz=880e6, tx_stop_hz=925e6)
            db.add_transmitter(
                Assignment(
                    id=None,
                    name="Базовая станция 1",
                    tx_frequency_hz=901.25e6,
                    bandwidth_hz=200e3,
                    owner_id=owner_id,
                    band_type_id=band_id,
                    latitude=55.75,
                    longitude=37.61,
                )
            )
            items = db.assignments()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].owner_name, "Оператор связи")
            self.assertEqual(items[0].band_type_name, "GSM 900")

    def test_csv_roundtrip(self):
        import tempfile

        from radioinspector.assignments import Assignment, AssignmentDatabase

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "in.csv"
            csv_path.write_text(
                "name,frequency_hz,bandwidth_hz,owner,band_type\n"
                "Репитер,946e6,100000,Оператор,Радиовещание\n",
                encoding="utf-8",
            )
            with AssignmentDatabase() as db:
                count = db.import_csv(csv_path)
                self.assertEqual(count, 1)
                out = db.export_csv(Path(tmp) / "out.csv")
                self.assertTrue(out.exists())
                self.assertIn("Репитер", out.read_text(encoding="utf-8"))


class TestOccupancy(unittest.TestCase):
    def test_channel_and_band_occupancy(self):
        from radioinspector.occupancy import band_occupancy

        freqs = np.arange(4) * 1000.0
        grid = np.array(
            [
                [-90.0, -90.0, -90.0, -90.0],
                [-20.0, -20.0, -90.0, -90.0],
                [-20.0, -20.0, -90.0, -90.0],
            ]
        )
        result = band_occupancy(grid, freqs, threshold_db=-50.0)
        self.assertEqual(result.channel_count, 4)
        self.assertEqual(result.occupied_channels, 2)
        self.assertAlmostEqual(result.band_occupancy, 0.5)
        self.assertAlmostEqual(result.channel_occupancy[0], 2.0 / 3.0)


class TestInterference(unittest.TestCase):
    def test_harmonic_detection(self):
        from radioinspector.interference import check_harmonics

        freqs = np.linspace(0, 500e6, 5001)
        levels = np.full_like(freqs, -120.0)
        mask = np.abs(freqs - 200e6) <= 100e3
        levels[mask] = -30.0
        trace = Trace(freqs, levels)

        results = check_harmonics(trace, 100e6, count=3, threshold_db=-80.0)
        second = [r for r in results if abs(r.predicted_hz - 200e6) < 1]
        self.assertTrue(second)
        self.assertTrue(second[0].detected)

    def test_intermodulation_products_set(self):
        from radioinspector.interference import intermodulation_products

        products = intermodulation_products(100e6, 110e6, 3)
        self.assertIn(90e6, products)   # 2f1 - f2
        self.assertIn(120e6, products)  # 2f2 - f1
        self.assertIn(310e6, products)  # 2f1 + f2
        self.assertNotIn(10e6, products)  # f2 - f1 относится ко 2-му порядку

    def test_second_order_products(self):
        from radioinspector.interference import intermodulation_products

        products = intermodulation_products(100e6, 110e6, 2)
        self.assertIn(210e6, products)  # f1 + f2
        self.assertIn(10e6, products)   # f2 - f1


class TestGeoloc(unittest.TestCase):
    def test_haversine(self):
        from radioinspector.geoloc import haversine_m

        distance = haversine_m(55.75, 37.61, 55.76, 37.61)
        self.assertAlmostEqual(distance, 1112.0, delta=60.0)

    def test_coverage_map_grid(self):
        from radioinspector.geoloc import MeasurementPoint, build_coverage_map

        points = [
            MeasurementPoint(latitude=55.75, longitude=37.61, level_db=-50.0),
            MeasurementPoint(latitude=55.76, longitude=37.62, level_db=-60.0),
            MeasurementPoint(latitude=55.77, longitude=37.63, level_db=-40.0),
        ]
        coverage = build_coverage_map(points, rows=5, cols=5)
        self.assertEqual(len(coverage.cells), 25)
        self.assertIsNotNone(coverage.max_level_db)
        self.assertEqual(coverage.max_level_db, -40.0)

    def test_triangulation_recovers_source(self):
        import math

        from radioinspector.geoloc import Bearing, triangulate_bearings

        source = (55.75, 37.65)
        observers = [(55.75, 37.60), (55.76, 37.62), (55.74, 37.68)]

        def _bearing(lat: float, lon: float) -> float:
            d_east = (37.65 - lon) * math.cos(math.radians(source[0]))
            d_north = source[0] - lat
            return (math.degrees(math.atan2(d_east, d_north))) % 360.0

        bearings = [
            Bearing(lat, lon, _bearing(lat, lon)) for lat, lon in observers
        ]
        fix = triangulate_bearings(bearings)
        self.assertIsNotNone(fix)
        self.assertAlmostEqual(fix.latitude, source[0], delta=0.005)
        self.assertAlmostEqual(fix.longitude, source[1], delta=0.005)

    def test_triangulation_requires_three_bearings(self):
        from radioinspector.geoloc import Bearing, triangulate_bearings

        self.assertIsNone(triangulate_bearings([Bearing(55.0, 37.0, 10.0)]))

    def test_declination_conversion(self):
        from radioinspector.geoloc import Bearing

        bearing = Bearing(55.0, 37.0, 100.0, declination_deg=7.0)
        self.assertAlmostEqual(bearing.true_azimuth_deg, 107.0)


class TestExports(unittest.TestCase):
    def test_cef_export(self):
        import tempfile

        from radioinspector.export import export_cef
        from radioinspector.panorama import PanoramaStore, ScanSettings

        settings = ScanSettings(
            start_hz=100e6, stop_hz=101e6, points=11, bandwidth_hz=100e3
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.pan"
            with PanoramaStore(path, settings) as store:
                store.append(make_trace(np.arange(11) * 1000.0, np.full(11, -40.0)))
                out = export_cef(store, Path(tmp) / "out.cef")
            content = out.read_text(encoding="ascii")
        self.assertIn("FileType: CMF", content)
        self.assertIn("FreqStart:", content)
        self.assertIn("DataPoints: 11", content)

    def test_csv_export(self):
        import tempfile

        from radioinspector.export import export_trace_csv

        with tempfile.TemporaryDirectory() as tmp:
            out = export_trace_csv(
                make_trace([1.0, 2.0], [-10.0, -20.0]), Path(tmp) / "t.csv"
            )
            content = out.read_text(encoding="utf-8")
        self.assertIn("frequency_hz", content)

    def test_sigmf_export(self):
        import tempfile

        from radioinspector.export import export_sigmf

        with tempfile.TemporaryDirectory() as tmp:
            data_path, meta_path = export_sigmf(
                make_trace(np.arange(10) * 1000.0, np.full(10, -30.0)),
                Path(tmp) / "rec",
            )
            meta = meta_path.read_text(encoding="utf-8")
            self.assertTrue(data_path.exists())
            self.assertTrue(data_path.stat().st_size > 0)
        self.assertIn("core:datatype", meta)
        self.assertIn("core:sample_rate", meta)
        self.assertIn("captures", meta)


class TestReporting(unittest.TestCase):
    def test_render_substitution(self):
        from radioinspector.reporting import ReportContext

        context = ReportContext({"name": "ABC", "value": 42})
        rendered = context.render("Прибор {{name}}, значение {{value}}, пусто {{other}}")
        self.assertIn("Прибор ABC", rendered)
        self.assertIn("значение 42", rendered)
        self.assertIn("н/д", rendered)

    def test_template_keys(self):
        from radioinspector.reporting import ProtocolTemplate

        template = ProtocolTemplate("t", "{{a}} и {{b}} и {{a}}")
        self.assertEqual(template.keys(), ["a", "b"])


class TestCli(unittest.TestCase):
    def test_demo_command_creates_database(self):
        import tempfile

        from radioinspector.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            code = main(["demo", "--output", tmp, "--cycles", "3"])
            self.assertEqual(code, 0)
            stores = list(Path(tmp).glob("*.pan"))
            self.assertEqual(len(stores), 1)
            self.assertTrue((Path(tmp) / "signals.json").exists())

    def test_convert_command(self):
        from radioinspector.cli import main

        self.assertEqual(main(["convert", "30", "--from", "dBm", "--to", "mW"]), 0)

    def test_devices_command(self):
        from radioinspector.cli import main

        self.assertEqual(main(["devices"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
