"""Оценка загрузки и эффективности использования радиочастотного спектра.

Реализует процедуру, принятую в рекомендации ITU-R SM.1880 «Spectrum occupancy
measurement and evaluation»: на каждой частотной точке сохраняется измеренный
уровень, а постобработкой определяется процент времени, в течение которого
уровень превышает заданный порог. Загрузка полосы определяется как доля
частот (или каналов), в которых зафиксировано превышение порога.

Для полосы частот загрузка вычисляется как отношение числа занятых каналов к
общему числу каналов; для отдельной частотной точки — как отношение времени
превышения порога к общему времени наблюдения.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .panorama import PanoramaStore
from .trace import Trace


@dataclass
class OccupancyResult:
    """Результат оценки загрузки диапазона."""

    start_hz: float
    stop_hz: float
    threshold_db: float
    channel_count: int
    occupied_channels: int
    band_occupancy: float
    channel_occupancy: list[float] = field(default_factory=list)
    channel_frequencies: list[float] = field(default_factory=list)
    mean_level_db: float = float("nan")
    max_level_db: float = float("nan")
    observation_time_s: float = 0.0

    def as_dict(self) -> dict:
        return {
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "threshold_db": self.threshold_db,
            "channel_count": self.channel_count,
            "occupied_channels": self.occupied_channels,
            "band_occupancy": self.band_occupancy,
            "channel_occupancy": list(self.channel_occupancy),
            "channel_frequencies": list(self.channel_frequencies),
            "mean_level_db": self.mean_level_db,
            "max_level_db": self.max_level_db,
            "observation_time_s": self.observation_time_s,
        }


def channel_occupancy(
    grid: np.ndarray,
    frequencies: np.ndarray,
    threshold_db: float,
) -> tuple[np.ndarray, float]:
    """Занятость каждой частотной точки по времени превышения порога.

    Параметры
    ----------
    grid:
        Матрица уровней «панорамы × частота», дБ.
    frequencies:
        Частоты столбцов сетки, Гц.
    threshold_db:
        Порог превышения, дБ.

    Возвращает
    -----------
    occupancy:
        Доля времени превышения порога для каждой точки, от 0 до 1.
    frequency:
        Частоты точек, Гц.
    """
    if grid.size == 0:
        return np.array([]), frequencies
    exceeded = grid > threshold_db
    occupancy = exceeded.mean(axis=0)
    return occupancy, frequencies


def band_occupancy(
    grid: np.ndarray,
    frequencies: np.ndarray,
    threshold_db: float,
) -> OccupancyResult:
    """Загрузка диапазона частот и эффективность его использования.

    Параметры
    ----------
    grid:
        Матрица уровней «панорамы × частота», дБ.
    frequencies:
        Частоты столбцов сетки, Гц.
    threshold_db:
        Порог превышения, дБ.
    """
    if grid.size == 0:
        return OccupancyResult(0.0, 0.0, threshold_db, 0, 0, 0.0)

    occupancy, freqs = channel_occupancy(grid, frequencies, threshold_db)
    occupied_channels = int(np.count_nonzero(occupancy > 0.0))
    channel_count = int(freqs.size)
    band = occupied_channels / channel_count if channel_count else 0.0

    observation_time = 0.0
    if grid.shape[0] > 1:
        observation_time = float(grid.shape[0])  # один цикл сканирования на панораму

    return OccupancyResult(
        start_hz=float(freqs[0]),
        stop_hz=float(freqs[-1]),
        threshold_db=threshold_db,
        channel_count=channel_count,
        occupied_channels=occupied_channels,
        band_occupancy=band,
        channel_occupancy=[float(v) for v in occupancy],
        channel_frequencies=[float(v) for v in freqs],
        mean_level_db=float(np.mean(grid)),
        max_level_db=float(np.max(grid)),
        observation_time_s=observation_time,
    )


def occupancy_from_store(
    store: PanoramaStore,
    threshold_db: float,
    start_hz: float | None = None,
    stop_hz: float | None = None,
) -> OccupancyResult:
    """Оценка загрузки по сохранённой базе данных панорам."""
    grid, _times = store.level_grid()
    freqs = store.frequencies()
    if grid.size == 0:
        return OccupancyResult(0.0, 0.0, threshold_db, 0, 0, 0.0)
    if start_hz is not None or stop_hz is not None:
        mask = np.ones(freqs.size, dtype=bool)
        if start_hz is not None:
            mask &= freqs >= start_hz
        if stop_hz is not None:
            mask &= freqs <= stop_hz
        grid = grid[:, mask]
        freqs = freqs[mask]
    return band_occupancy(grid, freqs, threshold_db)


def busy_hour_occupancy(
    store: PanoramaStore,
    threshold_db: float,
) -> dict:
    """Занятость диапазона по часам суток.

    Возвращает словарь с номером часа (0–23) и средней долей занятости
    для соответствующего часа наблюдения.
    """
    grid, times = store.level_grid()
    if grid.size == 0:
        return {}
    occupancy, _freqs = channel_occupancy(grid, store.frequencies(), threshold_db)
    per_scan = float(np.mean(occupancy))
    buckets: dict[int, list[float]] = {}
    from datetime import datetime, timezone

    for stamp, value in zip(times, [per_scan] * len(times)):
        hour = datetime.fromtimestamp(stamp, tz=timezone.utc).hour
        buckets.setdefault(hour, []).append(value)
    return {hour: float(np.mean(values)) for hour, values in sorted(buckets.items())}


# ---------------------------------------------------------------------------
# Эффективность использования диапазона
# ---------------------------------------------------------------------------


@dataclass
class DutyInterval:
    """Интервал работы источника излучения в поддиапазоне частот.

    Соответствует записи вида «вкл. 19:16:06 11.04, выкл. 19:20:11 11.04,
    длит. 00:04:04» в протоколе исходного комплекса.
    """

    started_at: float
    ended_at: float
    active: bool

    @property
    def duration_s(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "active": self.active,
            "duration_s": self.duration_s,
        }


def _format_interval(seconds: float) -> str:
    """Формат интервала в стиле исходного комплекса: «27сек646мс»."""
    if seconds != seconds:  # NaN
        return "—"
    total_ms = int(round(seconds * 1000.0))
    return f"{total_ms // 1000}сек{total_ms % 1000:03d}мс"


@dataclass
class BandEfficiency:
    """Эффективность использования поддиапазона частот.

    Исходный комплекс определяет эффективность как долю моментов наблюдения,
    в которых уровень сигнала превышал порог: «Эффективность использования
    диапазона частот 8.6% (количество измерений, в которых амплитуда сигнала
    превышала порог n=1002, общее количество измерений N=11633)».

    Это иная величина, чем загрузка диапазона по каналам: загрузка отвечает
    на вопрос «сколько частотных позиций занято», эффективность — «какую долю
    времени сигнал присутствовал в эфире».
    """

    start_hz: float
    stop_hz: float
    threshold_db: float
    measurements: int
    exceedances: int
    efficiency: float
    min_level_db: float = float("nan")
    max_level_db: float = float("nan")
    mean_interval_s: float = float("nan")
    min_interval_s: float = float("nan")
    max_interval_s: float = float("nan")
    observation_time_s: float = 0.0
    intervals: list[DutyInterval] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "start_hz": self.start_hz,
            "stop_hz": self.stop_hz,
            "threshold_db": self.threshold_db,
            "measurements": self.measurements,
            "exceedances": self.exceedances,
            "efficiency": self.efficiency,
            "min_level_db": self.min_level_db,
            "max_level_db": self.max_level_db,
            "mean_interval_s": self.mean_interval_s,
            "min_interval_s": self.min_interval_s,
            "max_interval_s": self.max_interval_s,
            "observation_time_s": self.observation_time_s,
            "intervals": [item.as_dict() for item in self.intervals],
        }

    def statistics(self) -> str:
        """Статистические данные о работе источника излучения.

        Формат воспроизводит запись исходного комплекса:

        ``вкл. 19:16:06 11.04, выкл. 19:20:11 11.04, длит. 00:04:04``

        Перечисляются интервалы, в течение которых сигнал присутствовал
        в эфире; интервалы отсутствия сигнала в исходной протокол не
        включаются.
        """
        from datetime import datetime, timezone

        lines: list[str] = []
        for item in self.intervals:
            if not item.active:
                continue
            start = datetime.fromtimestamp(item.started_at, tz=timezone.utc)
            end = datetime.fromtimestamp(item.ended_at, tz=timezone.utc)
            duration = int(round(item.duration_s))
            lines.append(
                f"вкл. {start:%H:%M:%S %d.%m}, выкл. {end:%H:%M:%S %d.%m}, "
                f"длит. {duration // 3600:02d}:{duration % 3600 // 60:02d}:{duration % 60:02d}"
            )
        return "\n".join(lines)

    def summary(self) -> str:
        """Сводка результатов наблюдения в формате исходного комплекса.

        Воспроизводит блок «Результаты измерений использования частоты или
        частотных каналов»:

        ``Эффективность использования диапазона частот 8.6% (количество
        измерений, в которых амплитуда сигнала превышала порог n=1002,
        общее количество измерений N=11633).``
        """
        lines = [
            "Эффективность использования диапазона частот "
            f"{self.efficiency * 100:.1f}% (количество измерений, в которых "
            f"амплитуда сигнала превышала порог n={self.exceedances}, общее "
            f"количество измерений N={self.measurements}).",
            f"Количество измерений {self.measurements}.",
        ]
        if self.measurements > 1:
            lines.append(
                "Средний интервал между измерениями "
                f"{_format_interval(self.mean_interval_s)}  "
                "Минимальный интервал между измерениями "
                f"{_format_interval(self.min_interval_s)}  "
                "Максимальный интервал между измерениями "
                f"{_format_interval(self.max_interval_s)}"
            )
        statistics = self.statistics()
        if statistics:
            lines.append("Статистические данные о работе источника излучения:")
            lines.append(statistics)
        return "\n".join(lines)


def _band_level_series(
    grid: np.ndarray,
    frequencies: np.ndarray,
    start_hz: float | None,
    stop_hz: float | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Выборка столбцов поддиапазона и максимальный уровень в каждой панораме.

    Исходный комплекс при обработке результатов «производил поиск максимального
    значения измеренного уровня в указанном выше диапазоне частот», поэтому
    сигнал считается присутствующим в эфире, если хоть в одной частотной
    позиции поддиапазона уровень превысил порог.
    """
    mask = np.ones(frequencies.size, dtype=bool)
    if start_hz is not None:
        mask &= frequencies >= start_hz
    if stop_hz is not None:
        mask &= frequencies <= stop_hz
    selected = grid[:, mask]
    if selected.size == 0:
        return np.array([]), frequencies[mask]
    return selected.max(axis=1), frequencies[mask]


def duty_intervals(
    levels: np.ndarray,
    times: np.ndarray,
    threshold_db: float,
) -> list[DutyInterval]:
    """Разбиение временного ряда на интервалы присутствия и отсутствия сигнала."""
    if levels.size == 0 or times.size == 0:
        return []
    active = levels > threshold_db
    result: list[DutyInterval] = []
    start = 0
    for index in range(1, active.size):
        if active[index] != active[index - 1]:
            result.append(
                DutyInterval(
                    started_at=float(times[start]),
                    ended_at=float(times[index]),
                    active=bool(active[index - 1]),
                )
            )
            start = index
    end_time = float(times[-1])
    if times.size > 1:
        step = float(np.median(np.diff(times)))
        end_time += max(0.0, step)
    result.append(
        DutyInterval(
            started_at=float(times[start]),
            ended_at=end_time,
            active=bool(active[-1]),
        )
    )
    return result


def band_efficiency(
    grid: np.ndarray,
    times: np.ndarray,
    frequencies: np.ndarray,
    threshold_db: float,
    start_hz: float | None = None,
    stop_hz: float | None = None,
) -> BandEfficiency:
    """Эффективность использования поддиапазона частот и статистика интервалов."""
    if grid.size == 0:
        return BandEfficiency(
            start_hz=0.0,
            stop_hz=0.0,
            threshold_db=threshold_db,
            measurements=0,
            exceedances=0,
            efficiency=0.0,
        )

    levels, freqs = _band_level_series(grid, frequencies, start_hz, stop_hz)
    if levels.size == 0:
        return BandEfficiency(
            start_hz=0.0,
            stop_hz=0.0,
            threshold_db=threshold_db,
            measurements=0,
            exceedances=0,
            efficiency=0.0,
        )

    measurements = int(levels.size)
    exceedances = int(np.count_nonzero(levels > threshold_db))
    efficiency = exceedances / measurements if measurements else 0.0

    intervals: list[float] = []
    if times.size > 1:
        intervals = [float(value) for value in np.diff(times[:measurements])]

    observation = 0.0
    if times.size > 1:
        observation = float(times[-1] - times[0])
    elif times.size == 1:
        observation = 0.0

    return BandEfficiency(
        start_hz=float(freqs[0]),
        stop_hz=float(freqs[-1]),
        threshold_db=threshold_db,
        measurements=measurements,
        exceedances=exceedances,
        efficiency=efficiency,
        min_level_db=float(np.min(levels)),
        max_level_db=float(np.max(levels)),
        mean_interval_s=float(np.mean(intervals)) if intervals else float("nan"),
        min_interval_s=float(np.min(intervals)) if intervals else float("nan"),
        max_interval_s=float(np.max(intervals)) if intervals else float("nan"),
        observation_time_s=observation,
        intervals=duty_intervals(levels, times[:measurements], threshold_db),
    )


def efficiency_from_store(
    store: PanoramaStore,
    threshold_db: float,
    start_hz: float | None = None,
    stop_hz: float | None = None,
) -> BandEfficiency:
    """Эффективность использования диапазона по сохранённой базе данных панорам."""
    grid, times = store.level_grid()
    freqs = store.frequencies()
    return band_efficiency(grid, times, freqs, threshold_db, start_hz, stop_hz)

