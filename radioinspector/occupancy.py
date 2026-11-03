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
