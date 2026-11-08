"""Линия порога: редактирование, хранение и применение.

Реализует раздел 6 «Линия порога» руководства оператора Книги 6:

* в одной базе данных панорам хранятся три линии порога;
* линия отображается красной ломаной, вершины превышающих её сигналов
  подкрашиваются алой окантовкой;
* линия порога может быть построена по одному из текущих графиков панорам
  (максимумов, текущих значений, усреднённых значений);
* линия порога сохраняется в базу данных панорам и загружается обратно
  либо из файла трассы ``*.trd`` (требующего совпадения параметров
  сканирования), либо из файла линии порога ``*.thr`` (с интерполяцией
  по частоте);
* при создании новой базы данных панорам линия порога по умолчанию —
  горизонтальная прямая на уровне 2/3 вертикальной сетки экрана.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .trace import Trace
from .units import UnitError, normalize_unit

#: Число линий порога, хранимых в базе данных панорам.
THRESHOLD_LINES_COUNT = 3

#: Уровень линии порога по умолчанию относительно нижней границы сетки.
DEFAULT_THRESHOLD_FRACTION = 2.0 / 3.0


@dataclass
class ThresholdLine:
    """Редактируемая линия порога.

    Хранится как набор пар «частота — уровень»; при применении к спектру
    значения интерполируются по частоте.
    """

    frequencies: np.ndarray
    levels: np.ndarray
    index: int = 0
    unit: str = "dBm"

    def __post_init__(self) -> None:
        self.frequencies = np.asarray(self.frequencies, dtype=float)
        self.levels = np.asarray(self.levels, dtype=float)
        if self.frequencies.shape != self.levels.shape:
            raise ValueError("Массивы частот и уровней линии порога должны совпадать")

    def __len__(self) -> int:
        return int(self.levels.size)

    @property
    def empty(self) -> bool:
        return self.levels.size == 0

    def level_at(self, frequency: float) -> float:
        """Уровень линии порога в заданной частоте (интерполяция)."""
        if self.empty:
            return float("nan")
        if self.levels.size == 1:
            return float(self.levels[0])
        return float(np.interp(frequency, self.frequencies, self.levels))

    def evaluate(self, trace: Trace) -> np.ndarray:
        """Значения линии порога на частотах спектра."""
        if self.empty:
            return np.full(trace.size, float("nan"))
        return np.interp(trace.frequencies, self.frequencies, self.levels)

    def shifted(self, offset_db: float) -> "ThresholdLine":
        """Сдвиг всей линии по вертикали, дБ."""
        return ThresholdLine(
            self.frequencies.copy(), self.levels + offset_db, self.index, self.unit
        )

    def shifted_segment(
        self, f_start: float, f_stop: float, offset_db: float
    ) -> "ThresholdLine":
        """Сдвиг участка линии между двумя частотами по вертикали, дБ."""
        levels = self.levels.copy()
        mask = (self.frequencies >= f_start) & (self.frequencies <= f_stop)
        levels[mask] += offset_db
        return ThresholdLine(self.frequencies.copy(), levels, self.index, self.unit)

    def moved(self, delta_hz: float) -> "ThresholdLine":
        """Сдвиг линии по горизонтали, Гц."""
        return ThresholdLine(
            self.frequencies + delta_hz, self.levels.copy(), self.index, self.unit
        )

    def from_trace(self, trace: Trace, offset_db: float = 0.0) -> "ThresholdLine":
        """Построение линии порога по графику панорам."""
        return ThresholdLine(
            trace.frequencies.copy(), trace.levels + offset_db, self.index, trace.unit
        )

    @classmethod
    def horizontal(
        cls, start_hz: float, stop_hz: float, level_db: float, index: int = 0
    ) -> "ThresholdLine":
        """Горизонтальная линия порога на заданном уровне."""
        return cls(
            np.array([start_hz, stop_hz], dtype=float),
            np.array([level_db, level_db], dtype=float),
            index,
        )

    @classmethod
    def default_for(
        cls, trace: Trace, index: int = 0, fraction: float = DEFAULT_THRESHOLD_FRACTION
    ) -> "ThresholdLine":
        """Линия порога по умолчанию — горизонтальная прямая на уровне 2/3 сетки.

        Уровень сетки определяется как минимум уровня в панораме; прямая
        располагается на ``min + fraction * (max − min)``.
        """
        if trace.size == 0:
            return cls.horizontal(0.0, 0.0, 0.0, index)
        low = float(np.min(trace.levels))
        high = float(np.max(trace.levels))
        level = low + fraction * (high - low)
        return cls.horizontal(trace.start, trace.stop, level, index)

    def save(self, path: str | Path) -> Path:
        """Сохранение линии порога в файл ``*.thr`` (JSON)."""
        path = Path(path)
        payload = {
            "format": "openradioinspector-threshold",
            "version": 1,
            "index": self.index,
            "unit": self.unit,
            "frequencies": self.frequencies.tolist(),
            "levels": self.levels.tolist(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "ThresholdLine":
        """Загрузка линии порога из файла ``*.thr``."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            np.asarray(payload["frequencies"], dtype=float),
            np.asarray(payload["levels"], dtype=float),
            int(payload.get("index", 0)),
            payload.get("unit", "dBm"),
        )

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "unit": self.unit,
            "frequencies": self.frequencies.tolist(),
            "levels": self.levels.tolist(),
        }


def resample_threshold(
    line: ThresholdLine, trace: Trace
) -> ThresholdLine:
    """Пересчёт линии порога на частотную сетку панорамы.

    Соответствует интерполяции сохранённой линии к текущим параметрам
    сканирования, описанной в §6.2 руководства.
    """
    if line.empty:
        return line
    return ThresholdLine(
        trace.frequencies.copy(),
        np.interp(trace.frequencies, line.frequencies, line.levels),
        line.index,
        line.unit,
    )


def exceeded_intervals(
    trace: Trace, line: ThresholdLine
) -> list[tuple[float, float]]:
    """Интервалы частот, на которых сигнал превышает линию порога.

    Возвращает список пар (начало, конец) в Гц. Смежные точки с превышением
    объединяются в один интервал.
    """
    if trace.size == 0 or line.empty:
        return []
    threshold = line.evaluate(trace)
    mask = trace.levels > threshold
    intervals: list[tuple[float, float]] = []
    start: Optional[int] = None
    for idx, flag in enumerate(mask):
        if flag and start is None:
            start = idx
        elif not flag and start is not None:
            intervals.append((float(trace.frequencies[start]), float(trace.frequencies[idx - 1])))
            start = None
    if start is not None:
        intervals.append((float(trace.frequencies[start]), float(trace.frequencies[-1])))
    return intervals


def power_over_threshold(
    trace: Trace, line: ThresholdLine, interval: tuple[float, float]
) -> float:
    """Мощность сигнала, превышающая линию порога, Вт.

    Вычисляется как сумма мощностей точек интервала за вычетом мощности,
    соответствующей уровню линии порога. Уровни трактуются как мощности
    относительно 1 мВт, поэтому результат возвращается в ваттах — в тех
    же единицах, что и порог мощности превышения (0,5–20 ркВт в
    исходном комплексе).
    """
    trace_unit = normalize_unit(trace.unit)
    line_unit = normalize_unit(line.unit)
    if trace_unit not in ("dBm", "dBW") or line_unit != trace_unit:
        raise UnitError("Абсолютная мощность требует согласованных dBm или dBW; относительные шкалы без калибровки недоступны")
    f_start, f_stop = interval
    segment = trace.slice(f_start, f_stop)
    if segment.size == 0:
        return 0.0
    threshold = line.evaluate(segment)
    signal_mw = np.power(10.0, segment.levels / 10.0)
    threshold_mw = np.power(10.0, threshold / 10.0)
    excess = np.clip(signal_mw - threshold_mw, 0.0, None)
    return float(np.sum(excess)) / (1000.0 if trace_unit == "dBm" else 1.0)
