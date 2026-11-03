"""Поиск источников помех.

Реализует проверку гипотез о наличии в измеренном спектре:

* гармоник передатчика (кратные частоты основного излучения);
* субгармоник (дробные частоты основного излучения);
* интермодуляционных продуктов 2-го, 3-го, 5-го и 7-го порядков.

Порядок интермодуляционного продукта определяется как ``n = m1 + m2 + …``
(рекомендация ITU-R SM.1446 «Definition and measurement of intermodulation
distortion products»). Для пары частот ``f1`` и ``f2``:

* 2-й порядок: ``f1 ± f2``;
* 3-й порядок: ``2f1 ± f2``, ``2f2 ± f1``;
* 5-й порядок: ``3f1 ± 2f2``, ``2f1 ± 3f2``, ``4f1 ± f2``, ``f1 ± 4f2``;
* 7-й порядок: ``5f1 ± 2f2``, ``4f1 ± 3f2``, ``3f1 ± 4f2``, ``2f1 ± 5f2``,
  ``6f1 ± f2``, ``f1 ± 6f2``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from .trace import Trace

#: Порядки интермодуляции, проверяемые по умолчанию.
DEFAULT_IM_ORDERS = (2, 3, 5, 7)

#: Максимальное расхождение между расчётной и измеренной частотой, при
#: котором продукт считается обнаруженным, Гц.
DEFAULT_TOLERANCE_HZ = 25_000.0


@dataclass
class InterferenceHypothesis:
    """Результат проверки одной гипотезы."""

    kind: str
    source_frequencies_hz: list[float]
    predicted_hz: float
    order: Optional[int] = None
    detected: bool = False
    measured_hz: Optional[float] = None
    measured_level_db: Optional[float] = None
    threshold_db: Optional[float] = None
    excess_db: float = float("nan")

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "source_frequencies_hz": list(self.source_frequencies_hz),
            "predicted_hz": self.predicted_hz,
            "order": self.order,
            "detected": self.detected,
            "measured_hz": self.measured_hz,
            "measured_level_db": self.measured_level_db,
            "threshold_db": self.threshold_db,
            "excess_db": self.excess_db,
        }


def harmonic_frequencies(frequency_hz: float, count: int = 10) -> list[float]:
    """Частоты гармоник передатчика, Гц."""
    return [frequency_hz * n for n in range(2, count + 1)]


def subharmonic_frequencies(frequency_hz: float, count: int = 3) -> list[float]:
    """Частоты субгармоник передатчика, Гц."""
    return [frequency_hz / n for n in range(2, count + 1)]


def intermodulation_products(
    f1: float, f2: float, order: int
) -> list[float]:
    """Частоты интермодуляционных продуктов заданного порядка.

    Реализовано по правилу ``n = m1 + m2``, где ``m1 ≠ 0`` и ``m2 ≠ 0``;
    знаки комбинаций ``±`` перебираются для обоих слагаемых.
    """
    products: list[float] = []
    for m1 in range(1, order):
        m2 = order - m1
        for s1 in (1, -1):
            for s2 in (1, -1):
                value = s1 * m1 * f1 + s2 * m2 * f2
                if value <= 0:
                    continue
                if any(abs(value - p) < 1e-6 for p in products):
                    continue
                products.append(value)
    return sorted(products)


def _find_peak(
    trace: Trace, predicted_hz: float, tolerance_hz: float
) -> tuple[bool, Optional[float], Optional[float]]:
    """Поиск пика в окне вокруг расчётной частоты."""
    if trace.size == 0:
        return False, None, None
    mask = np.abs(trace.frequencies - predicted_hz) <= tolerance_hz
    if not np.any(mask):
        return False, None, None
    indices = np.flatnonzero(mask)
    peak_index = indices[int(np.argmax(trace.levels[indices]))]
    measured_hz = float(trace.frequencies[peak_index])
    measured_db = float(trace.levels[peak_index])
    return True, measured_hz, measured_db


def check_harmonics(
    trace: Trace,
    source_hz: float,
    count: int = 10,
    threshold_db: Optional[float] = None,
    tolerance_hz: float = DEFAULT_TOLERANCE_HZ,
) -> list[InterferenceHypothesis]:
    """Проверка гипотезы о наличии гармоник передатчика."""
    results: list[InterferenceHypothesis] = []
    for harmonic in harmonic_frequencies(source_hz, count):
        found, measured_hz, measured_db = _find_peak(trace, harmonic, tolerance_hz)
        detected = found and (threshold_db is None or measured_db >= threshold_db)
        results.append(
            InterferenceHypothesis(
                kind="harmonic",
                source_frequencies_hz=[source_hz],
                predicted_hz=harmonic,
                order=int(round(harmonic / source_hz)) if source_hz else None,
                detected=detected,
                measured_hz=measured_hz,
                measured_level_db=measured_db,
                threshold_db=threshold_db,
                excess_db=(measured_db - threshold_db)
                if (detected and threshold_db is not None and measured_db is not None)
                else float("nan"),
            )
        )
    return results


def check_subharmonics(
    trace: Trace,
    source_hz: float,
    count: int = 3,
    threshold_db: Optional[float] = None,
    tolerance_hz: float = DEFAULT_TOLERANCE_HZ,
) -> list[InterferenceHypothesis]:
    """Проверка гипотезы о наличии субгармоник передатчика."""
    results: list[InterferenceHypothesis] = []
    for sub in subharmonic_frequencies(source_hz, count):
        found, measured_hz, measured_db = _find_peak(trace, sub, tolerance_hz)
        detected = found and (threshold_db is None or measured_db >= threshold_db)
        results.append(
            InterferenceHypothesis(
                kind="subharmonic",
                source_frequencies_hz=[source_hz],
                predicted_hz=sub,
                order=None,
                detected=detected,
                measured_hz=measured_hz,
                measured_level_db=measured_db,
                threshold_db=threshold_db,
                excess_db=(measured_db - threshold_db)
                if (detected and threshold_db is not None and measured_db is not None)
                else float("nan"),
            )
        )
    return results


def check_intermodulation(
    trace: Trace,
    frequencies_hz: Sequence[float],
    orders: Sequence[int] = DEFAULT_IM_ORDERS,
    threshold_db: Optional[float] = None,
    tolerance_hz: float = DEFAULT_TOLERANCE_HZ,
) -> list[InterferenceHypothesis]:
    """Проверка гипотез об интермодуляционных продуктах.

    Для каждой пары частот из ``frequencies_hz`` перебираются произведения
    заданных порядков; для каждого расчётного значения выполняется поиск пика
    в пределах ``tolerance_hz``.
    """
    results: list[InterferenceHypothesis] = []
    freqs = list(frequencies_hz)
    for i, f1 in enumerate(freqs):
        for f2 in freqs[i + 1 :]:
            if abs(f1 - f2) < 1e-9:
                continue
            for order in orders:
                for product in intermodulation_products(f1, f2, order):
                    found, measured_hz, measured_db = _find_peak(
                        trace, product, tolerance_hz
                    )
                    detected = found and (
                        threshold_db is None or measured_db >= threshold_db
                    )
                    results.append(
                        InterferenceHypothesis(
                            kind="intermodulation",
                            source_frequencies_hz=[f1, f2],
                            predicted_hz=product,
                            order=order,
                            detected=detected,
                            measured_hz=measured_hz,
                            measured_level_db=measured_db,
                            threshold_db=threshold_db,
                            excess_db=(measured_db - threshold_db)
                            if (
                                detected
                                and threshold_db is not None
                                and measured_db is not None
                            )
                            else float("nan"),
                        )
                    )
    return results


def detect_interference(
    trace: Trace,
    source_hz: float,
    others_hz: Sequence[float] = (),
    threshold_db: Optional[float] = None,
    orders: Sequence[int] = DEFAULT_IM_ORDERS,
    tolerance_hz: float = DEFAULT_TOLERANCE_HZ,
) -> list[InterferenceHypothesis]:
    """Комплексная проверка: гармоники, субгармоники и интермодуляция."""
    hypotheses: list[InterferenceHypothesis] = []
    hypotheses.extend(
        check_harmonics(trace, source_hz, threshold_db=threshold_db, tolerance_hz=tolerance_hz)
    )
    hypotheses.extend(
        check_subharmonics(trace, source_hz, threshold_db=threshold_db, tolerance_hz=tolerance_hz)
    )
    if others_hz:
        hypotheses.extend(
            check_intermodulation(
                trace,
                [source_hz, *others_hz],
                orders=orders,
                threshold_db=threshold_db,
                tolerance_hz=tolerance_hz,
            )
        )
    return hypotheses
