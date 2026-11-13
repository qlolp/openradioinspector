"""Регрессионные проверки по находкам аудита кода от 13 ноября 2026.

Находки зафиксированы в ``docs/code-audit-2026-11-13.md``. Проверки закрывают
четыре группы дефектов: ``assert`` в рабочем коде, молчаливое распространение
нечисловых уровней, нестрогую сериализацию SigMF и отсутствие постраничной
выдачи кандидатов.
"""

from __future__ import annotations

import ast
import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from radioinspector.contracts import Candidate, CandidateState, TimeInterval
from radioinspector.measurements import (
    bandwidth_at_level,
    center_frequency_centroid,
    complex_center_frequency,
    control_bandwidth,
    field_strength,
    measure_signal,
    occupied_bandwidth_beta2,
)
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.trace import Trace
from radioinspector.web.server import page_limit, page_offset

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class AssertInProductionCodeTests(unittest.TestCase):
    """В рабочем коде не должно быть ``assert``: при ``python -O`` он снимается."""

    MODULES = (
        "radioinspector/cli.py",
        "radioinspector/archive_stream.py",
        "radioinspector/devices/rtl.py",
    )

    def test_no_assert_statements_in_listed_modules(self) -> None:
        for relative in self.MODULES:
            with self.subTest(module=relative):
                tree = ast.parse((REPOSITORY_ROOT / relative).read_text(encoding="utf-8"))
                asserts = [
                    node.lineno
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Assert)
                ]
                self.assertEqual(asserts, [], f"{relative}: assert в строках {asserts}")

    def test_no_assert_statements_in_package(self) -> None:
        offenders: list[str] = []
        for path in sorted((REPOSITORY_ROOT / "radioinspector").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    offenders.append(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], f"assert в рабочем коде: {offenders}")


class NonFiniteLevelTests(unittest.TestCase):
    """Нечисловые уровни должны отвергаться, а не давать результат NaN."""

    def _trace(self) -> Trace:
        return Trace(
            np.array([1_000_000_000.0, 1_500_000_000.0, 2_000_000_000.0]),
            np.array([-40.0, float("nan"), -60.0]),
        )

    def test_measurements_reject_non_finite_levels(self) -> None:
        trace = self._trace()
        calls = (
            lambda: occupied_bandwidth_beta2(trace),
            lambda: bandwidth_at_level(trace),
            lambda: control_bandwidth(trace),
            lambda: complex_center_frequency(trace),
            lambda: center_frequency_centroid(trace),
            lambda: field_strength(trace),
            lambda: measure_signal(trace),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaisesRegex(ValueError, "конечными числами"):
                    call()

    def test_finite_trace_still_measured(self) -> None:
        trace = Trace(
            np.array([1_000_000_000.0, 1_500_000_000.0, 2_000_000_000.0]),
            np.array([-40.0, -35.0, -60.0]),
        )
        result = occupied_bandwidth_beta2(trace)
        self.assertTrue(math.isfinite(result.bandwidth_hz))
        self.assertGreater(result.bandwidth_hz, 0.0)

    def test_field_strength_accepts_sequence(self) -> None:
        good = Trace(np.array([1e9, 2e9]), np.array([-40.0, -60.0]))
        self.assertTrue(math.isfinite(field_strength([good, good]).value))
        bad = Trace(np.array([1e9, 2e9]), np.array([-40.0, float("inf")]))
        with self.assertRaisesRegex(ValueError, "конечными числами"):
            field_strength([good, bad])


class SigmfStrictJsonTests(unittest.TestCase):
    """Метаданные SigMF должны быть строгим JSON: без NaN и Infinity."""

    def test_metadata_rejects_non_finite_center(self) -> None:
        from radioinspector.export.sigmf import export_sigmf

        # Нечисловая частота попадает в метаданные как NaN: строгий JSON
        # обязан отвергнуть такой файл, а не записать невалидный.
        trace = Trace(np.array([1e9, float("nan")]), np.array([-40.0, -60.0]))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                export_sigmf(trace, Path(directory) / "record.sigmf")

    def test_metadata_is_strict_json(self) -> None:
        from radioinspector.export.sigmf import export_sigmf

        trace = Trace(np.array([1e9, 2e9]), np.array([-40.0, -60.0]))
        with tempfile.TemporaryDirectory() as directory:
            _, meta_path = export_sigmf(trace, Path(directory) / "record.sigmf")
            payload = json.loads(
                meta_path.read_text(encoding="utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            self.assertIn("global", payload)


class CandidatePaginationTests(unittest.TestCase):
    """Постраничная выдача кандидатов: порядок, непересечение, границы."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.store = SQLiteSessionStore(str(Path(self._directory.name) / "audit.sqlite"))
        self.service = SessionService(store=self.store)
        session = self.service.create_session(
            {
                "name": "Аудит",
                "streams": [
                    {
                        "device": "tinySA Ultra",
                        "role": "spectrum_analyzer",
                        "level_unit": "dBm",
                        "port": "sim://tinysa",
                        "capabilities": ["sweep"],
                        "settings": {
                            "adapter": "tinysa-sim",
                            "start_hz": 100_000_000,
                            "stop_hz": 106_000_000,
                            "points": 61,
                            "period_s": 0.01,
                        },
                    }
                ],
            }
        )
        self.session_id = session.id
        self.stream_id = session.streams[0].id
        self.now = session.created_at
        interval = TimeInterval(started_at=self.now, ended_at=self.now)
        for index in range(50):
            self.store.save_candidate(
                Candidate(
                    id=f"cand-{index:04d}",
                    session_id=self.session_id,
                    source_stream_id=self.stream_id,
                    center_frequency_hz=100_000_000 + index * 1000,
                    level_unit="dBm",
                    interval=interval,
                    peak_level=-70.0,
                    state=CandidateState.NEW,
                )
            )

    def test_full_list_is_unchanged_without_limit(self) -> None:
        items = self.service.list_candidates(self.session_id)
        self.assertEqual(len(items), 50)
        self.assertEqual([item.id for item in items[:3]], ["cand-0000", "cand-0001", "cand-0002"])

    def test_pages_do_not_overlap_and_keep_order(self) -> None:
        first = self.service.list_candidates(self.session_id, limit=10)
        second = self.service.list_candidates(self.session_id, limit=10, offset=10)
        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 10)
        self.assertFalse({item.id for item in first} & {item.id for item in second})
        self.assertEqual(first[-1].id, "cand-0009")
        self.assertEqual(second[0].id, "cand-0010")

    def test_offset_beyond_end_returns_empty(self) -> None:
        self.assertEqual(self.service.list_candidates(self.session_id, limit=10, offset=100), [])

    def test_invalid_window_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.list_candidates(self.session_id, limit=0)
        with self.assertRaises(ValueError):
            self.service.list_candidates(self.session_id, offset=-1)

    def test_store_window_matches_service(self) -> None:
        page = self.store.load_candidates(session_id=self.session_id, limit=5, offset=5)
        self.assertEqual([item.id for item in page], [f"cand-{i:04d}" for i in range(5, 10)])
        self.assertEqual(self.store.count_candidates(session_id=self.session_id), 50)


class PageParameterTests(unittest.TestCase):
    """Разбор параметров ``limit`` и ``offset`` в HTTP-запросе."""

    def test_absent_parameters_mean_full_list(self) -> None:
        self.assertIsNone(page_limit(None))
        self.assertIsNone(page_limit(""))
        self.assertEqual(page_offset(None), 0)
        self.assertEqual(page_offset(""), 0)

    def test_valid_values(self) -> None:
        self.assertEqual(page_limit("100"), 100)
        self.assertEqual(page_offset("250"), 250)

    def test_invalid_values_are_rejected(self) -> None:
        for value in ("abc", "-1", "0", "1.5", "1e3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    page_limit(value)
        for value in ("abc", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    page_offset(value)

    def test_upper_bound_is_enforced(self) -> None:
        from radioinspector.request_limits import MAX_PAGE_LIMIT

        self.assertEqual(page_limit(str(MAX_PAGE_LIMIT)), MAX_PAGE_LIMIT)
        with self.assertRaises(ValueError):
            page_limit(str(MAX_PAGE_LIMIT + 1))


if __name__ == "__main__":
    unittest.main()
