"""Модель спектрального графика (амплитуда — частота).

Реализует правила работы с полем графиков панорам, описанные в разделе 8
«Поле отображения графиков панорам и спектрограмм» руководства оператора
Книги 6:

* масштабирование по оси X с правилом «поиск максимума внутри корзины»
  (§8.1.2) — при числе точек измерений больше числа точек отображения;
* линейная интерполяция при числе точек измерений меньше числа точек
  отображения;
* привязка курсоров и маркеров к точкам измерения с округлением до
  ближайшей точки массива панорам.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

#: Уровень «бесконечный шум», используемый при маскировании значений.
FLOOR_DB = -300.0


@dataclass
class Trace:
    """Амплитудно-частотная характеристика.

    Атрибуты
    ---------
    frequencies:
        Частоты точек измерения, Гц. Монотонно возрастают.
    levels:
        Уровни точек измерения, дБ (относительно 1 мВт для мощностных
        измерений).
    unit:
        Обозначение единицы уровней.
    """

    frequencies: np.ndarray
    levels: np.ndarray
    unit: str = "dBm"

    def __post_init__(self) -> None:
        self.frequencies = np.asarray(self.frequencies, dtype=float)
        self.levels = np.asarray(self.levels, dtype=float)
        if self.frequencies.shape != self.levels.shape:
            raise ValueError("Массивы частот и уровней должны иметь одинаковую длину")
        if self.frequencies.size and np.any(np.diff(self.frequencies) < 0):
            raise ValueError("Частоты должны идти в неубывающем порядке")

    # -- базовые свойства ---------------------------------------------------

    def __len__(self) -> int:
        return int(self.levels.size)

    @property
    def size(self) -> int:
        return int(self.levels.size)

    @property
    def empty(self) -> bool:
        return self.levels.size == 0

    @property
    def start(self) -> float:
        """Начальная частота, Гц."""
        return float(self.frequencies[0]) if self.size else float("nan")

    @property
    def stop(self) -> float:
        """Конечная частота, Гц."""
        return float(self.frequencies[-1]) if self.size else float("nan")

    @property
    def span(self) -> float:
        """Ширина полосы обзора, Гц."""
        return self.stop - self.start if self.size else float("nan")

    @property
    def step(self) -> float:
        """Шаг перестройки частоты, Гц."""
        if self.size < 2:
            return float("nan")
        return float(np.median(np.diff(self.frequencies)))

    @property
    def peak(self) -> float:
        """Максимальный уровень, дБ."""
        return float(np.max(self.levels)) if self.size else float("nan")

    # -- выборка и интерполяция --------------------------------------------

    def nearest_index(self, frequency: float) -> int:
        """Индекс точки измерения, ближайшей к заданной частоте.

        Соответствует правилу «округление до частоты ближайшей точки
        измерения», применяемому во всех частотных операциях.
        """
        if not self.size:
            raise IndexError("Пустой спектр")
        idx = int(np.argmin(np.abs(self.frequencies - frequency)))
        return idx

    def value_at(self, frequency: float) -> float:
        """Уровень в заданной точке, привязанный к ближайшей точке измерения."""
        return float(self.levels[self.nearest_index(frequency)])

    def value_at_interpolated(self, frequency: float) -> float:
        """Линейно интерполированный уровень (в дБ) для произвольной частоты."""
        if not self.size:
            raise IndexError("Пустой спектр")
        if self.size == 1:
            return float(self.levels[0])
        return float(np.interp(frequency, self.frequencies, self.levels))

    def slice(self, f_start: float, f_stop: float) -> "Trace":
        """Поддиапазон частот с округлением границ до точек измерения."""
        if not self.size:
            return Trace(np.array([]), np.array([]), self.unit)
        i0 = self.nearest_index(f_start)
        i1 = self.nearest_index(f_stop)
        if i1 < i0:
            i0, i1 = i1, i0
        return Trace(
            self.frequencies[i0 : i1 + 1].copy(),
            self.levels[i0 : i1 + 1].copy(),
            self.unit,
        )

    def decimate(self, target_points: int) -> "Trace":
        """Масштабирование по оси X до заданного числа точек отображения.

        Реализует алгоритм §8.1.2: если точек измерения больше, чем точек
        отображения, из каждой корзины выбирается максимум; иначе точки
        сохраняются, а соседние соединяются отрезками при отображении.
        """
        if target_points <= 0:
            raise ValueError("Число точек отображения должно быть положительным")
        if self.size <= target_points or self.size == 0:
            return Trace(self.frequencies.copy(), self.levels.copy(), self.unit)

        bucket = int(np.ceil(self.size / target_points))
        # Every source bin belongs to a bucket, including the partial tail.
        indices = np.array([
            start + int(np.argmax(self.levels[start : start + bucket]))
            for start in range(0, self.size, bucket)
        ])
        levels = self.levels[indices]
        freqs = self.frequencies[indices]
        return Trace(freqs, levels, self.unit)

    def resample_to(self, frequencies: Sequence[float]) -> "Trace":
        """Пересчёт на заданную сетку частот (линейная интерполяция)."""
        freqs = np.asarray(frequencies, dtype=float)
        if self.size == 0:
            return Trace(freqs, np.full(freqs.shape, FLOOR_DB), self.unit)
        return Trace(freqs, np.interp(freqs, self.frequencies, self.levels), self.unit)

    # -- преобразования уровней --------------------------------------------

    def with_offset_db(self, offset: float) -> "Trace":
        """Сдвиг всех уровней на постоянную величину, дБ."""
        return Trace(self.frequencies.copy(), self.levels + offset, self.unit)

    def combine(self, other: "Trace", method: str = "max") -> "Trace":
        """Поэлементное объединение с другой характеристикой.

        Методы: ``max``, ``min``, ``average``.
        """
        if self.size != other.size:
            raise ValueError("Характеристики должны иметь одинаковое число точек")
        if method == "max":
            levels = np.maximum(self.levels, other.levels)
        elif method == "min":
            levels = np.minimum(self.levels, other.levels)
        elif method in ("average", "avg"):
            levels = (self.levels + other.levels) / 2.0
        else:
            raise ValueError(f"Неизвестный метод объединения: {method}")
        return Trace(self.frequencies.copy(), levels, self.unit)

    def mask_below(self, threshold_db: float) -> "Trace":
        """Заменяет уровни ниже порога на значение «бесконечный шум»."""
        levels = np.where(self.levels < threshold_db, FLOOR_DB, self.levels)
        return Trace(self.frequencies.copy(), levels, self.unit)

    # -- служебное ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "unit": self.unit,
            "frequencies": self.frequencies.tolist(),
            "levels": self.levels.tolist(),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Trace":
        return cls(
            np.asarray(payload["frequencies"], dtype=float),
            np.asarray(payload["levels"], dtype=float),
            payload.get("unit", "dBm"),
        )


def traces_max(traces: Iterable[Trace]) -> Trace:
    """Поэлементный максимум набора характеристик."""
    traces = list(traces)
    if not traces:
        raise ValueError("Не задано ни одной характеристики")
    result = traces[0]
    for tr in traces[1:]:
        result = result.combine(tr, "max")
    return result


def traces_min(traces: Iterable[Trace]) -> Trace:
    """Поэлементный минимум набора характеристик."""
    traces = list(traces)
    if not traces:
        raise ValueError("Не задано ни одной характеристики")
    result = traces[0]
    for tr in traces[1:]:
        result = result.combine(tr, "min")
    return result


def traces_average(traces: Iterable[Trace]) -> Trace:
    """Поэлементное среднее набора характеристик."""
    traces = list(traces)
    if not traces:
        raise ValueError("Не задано ни одной характеристики")
    stack = np.vstack([tr.levels for tr in traces])
    return Trace(
        traces[0].frequencies.copy(), stack.mean(axis=0), traces[0].unit
    )


def db_difference(a: Trace, b: Trace) -> Trace:
    """Разность двух панорам (a − b) в дБ."""
    if a.size != b.size:
        raise ValueError("Характеристики должны иметь одинаковое число точек")
    return Trace(a.frequencies.copy(), a.levels - b.levels, a.unit)
