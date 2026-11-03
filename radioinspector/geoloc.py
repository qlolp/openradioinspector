"""Геоинформационная обработка результатов измерений.

Реализует раздел 5 «Геоинформационная система. Работа с электронной картой»
руководства оператора Книги 6:

* построение карт распределения напряжённости поля (§5.3.2): область
  анализа определяется крайними точками измерений, разбивается на ячейки с
  одинаковым шагом, для ячеек с несколькими измерениями выбирается среднее,
  минимальное или максимальное значение, а для пустых ячеек выполняется
  интерполяция по соседним ячейкам с измерениями;
* отображение результатов амплитудной пеленгации (§5.2.1): пеленги
  задаются магнитным азимутом, при расчёте точки пересечения учитывается
  магнитное склонение; пеленги снимаются не менее чем в трёх точках,
  расположенных на максимально возможном удалении друг от друга.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np

#: Радиус Земли, м (средний радиус по WGS-84).
EARTH_RADIUS_M = 6_371_000.0

#: Табличные значения магнитного склонения для крупных регионов, градусы.
#: Используются при выборе режима «стандартное магнитное склонение».
STANDARD_DECLINATION_DEG = {
    "moscow": 7.0,
    "yakutsk": -17.0,
    "murmansk": 14.0,
    "novosibirsk": 12.0,
    "vladivostok": -13.0,
    "kazan": 10.0,
    "ekaterinburg": 15.0,
    "rostov": 6.0,
    "omsk": 13.0,
    "irkutsk": -6.0,
}


@dataclass
class MeasurementPoint:
    """Точка измерения с привязкой к местности."""

    latitude: float
    longitude: float
    level_db: float = float("nan")
    frequency_hz: Optional[float] = None
    altitude_m: Optional[float] = None
    speed: Optional[float] = None
    heading: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "level_db": self.level_db,
            "frequency_hz": self.frequency_hz,
            "altitude_m": self.altitude_m,
            "speed": self.speed,
            "heading": self.heading,
        }


@dataclass
class Bearing:
    """Пеленг на источник излучения."""

    latitude: float
    longitude: float
    magnetic_azimuth_deg: float
    declination_deg: float = 0.0

    @property
    def true_azimuth_deg(self) -> float:
        """Истинный азимут с учётом магнитного склонения.

        Магнитное склонение считается положительным, если северный конец
        магнитной стрелки отклонён к востоку от географического меридиана.
        """
        return (self.magnetic_azimuth_deg + self.declination_deg) % 360.0

    def as_dict(self) -> dict:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "magnetic_azimuth_deg": self.magnetic_azimuth_deg,
            "declination_deg": self.declination_deg,
            "true_azimuth_deg": self.true_azimuth_deg,
        }


def standard_declination(region: str) -> Optional[float]:
    """Табличное значение магнитного склонения для региона, градусы."""
    return STANDARD_DECLINATION_DEG.get(region.strip().lower())


# --------------------------------------------------------------------------
# Геодезические помощники
# --------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками по большому кругу, м."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def destination_point(
    lat: float, lon: float, bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    """Смещение от точки на расстояние по азимуту.

    Возвращает широту и долготу конечной точки.
    """
    delta = distance_m / EARTH_RADIUS_M
    theta = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lambda1 = math.radians(lon)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta)
        + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lambda2) + 540.0) % 360.0 - 180.0


# --------------------------------------------------------------------------
# Карта распределения напряжённости поля
# --------------------------------------------------------------------------

@dataclass
class CoverageCell:
    """Ячейка карты покрытия."""

    row: int
    col: int
    latitude: float
    longitude: float
    level_db: float = float("nan")
    interpolated: bool = False
    sample_count: int = 0

    def as_dict(self) -> dict:
        return {
            "row": self.row,
            "col": self.col,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "level_db": self.level_db,
            "interpolated": self.interpolated,
            "sample_count": self.sample_count,
        }


@dataclass
class CoverageMap:
    """Карта распределения напряжённости поля."""

    cells: list[CoverageCell] = field(default_factory=list)
    rows: int = 0
    cols: int = 0
    start_latitude: float = 0.0
    start_longitude: float = 0.0
    step_latitude: float = 0.0
    step_longitude: float = 0.0
    min_level_db: float = float("nan")
    max_level_db: float = float("nan")

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "cols": self.cols,
            "start_latitude": self.start_latitude,
            "start_longitude": self.start_longitude,
            "step_latitude": self.step_latitude,
            "step_longitude": self.step_longitude,
            "min_level_db": self.min_level_db,
            "max_level_db": self.max_level_db,
            "cells": [cell.as_dict() for cell in self.cells],
        }

    def to_geojson(self) -> dict:
        """Представление карты в формате GeoJSON (Point-объекты)."""
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [cell.longitude, cell.latitude],
                    },
                    "properties": {
                        "level_db": cell.level_db,
                        "interpolated": cell.interpolated,
                        "sample_count": cell.sample_count,
                        "row": cell.row,
                        "col": cell.col,
                    },
                }
                for cell in self.cells
            ],
        }


def build_coverage_map(
    points: Sequence[MeasurementPoint],
    rows: int = 50,
    cols: int = 50,
    aggregation: str = "mean",
    search_radius_cells: int = 5,
) -> CoverageMap:
    """Построение карты распределения напряжённости поля.

    Алгоритм (§5.3.2):

    1. Область анализа определяется крайними западной, восточной, северной и
       южной точками измерений.
    2. Область разбивается на ячейки с одинаковым шагом по вертикали и
       горизонтали.
    3. Если в ячейку попало несколько точек, значение напряжённости вычисляется
       в соответствии с выбранными условиями: усреднённое, минимальное или
       максимальное.
    4. Для каждой ячейки без измерений выполняется поиск соседних ячеек с
       измерениями в пределах заданного радиуса, по найденным значениям
       выполняется интерполяция.

    Параметр ``search_radius_cells`` задаёт радиус поиска в ячейках; в
    оригинале он задаётся в процентах от общего числа ячеек.
    """
    valid = [p for p in points if p.level_db == p.level_db]
    result = CoverageMap()
    if not valid:
        return result

    lats = [p.latitude for p in valid]
    lons = [p.longitude for p in valid]
    north, south = max(lats), min(lats)
    west, east = min(lons), max(lons)

    rows = max(1, int(rows))
    cols = max(1, int(cols))
    step_lat = (north - south) / rows if north != south else 1e-4
    step_lon = (east - west) / cols if east != west else 1e-4

    buckets: dict[tuple[int, int], list[float]] = {}
    for point in valid:
        row = int((point.latitude - south) / step_lat) if step_lat else 0
        col = int((point.longitude - west) / step_lon) if step_lon else 0
        row = min(max(row, 0), rows - 1)
        col = min(max(col, 0), cols - 1)
        buckets.setdefault((row, col), []).append(point.level_db)

    levels = np.full((rows, cols), float("nan"))
    counts = np.zeros((rows, cols), dtype=int)

    for (row, col), values in buckets.items():
        if aggregation == "max":
            levels[row, col] = max(values)
        elif aggregation == "min":
            levels[row, col] = min(values)
        else:
            levels[row, col] = sum(values) / len(values)
        counts[row, col] = len(values)

    # Интерполяция пустых ячеек по соседям.
    if search_radius_cells > 0:
        for row in range(rows):
            for col in range(cols):
                if not math.isnan(levels[row, col]):
                    continue
                neighbours: list[float] = []
                for d_row in range(-search_radius_cells, search_radius_cells + 1):
                    for d_col in range(-search_radius_cells, search_radius_cells + 1):
                        if d_row == 0 and d_col == 0:
                            continue
                        r = row + d_row
                        c = col + d_col
                        if 0 <= r < rows and 0 <= c < cols and not math.isnan(levels[r, c]):
                            neighbours.append(levels[r, c])
                if neighbours:
                    levels[row, col] = sum(neighbours) / len(neighbours)
                    counts[row, col] = 0

    cells: list[CoverageCell] = []
    for row in range(rows):
        for col in range(cols):
            latitude = south + (row + 0.5) * step_lat
            longitude = west + (col + 0.5) * step_lon
            cells.append(
                CoverageCell(
                    row=row,
                    col=col,
                    latitude=latitude,
                    longitude=longitude,
                    level_db=float(levels[row, col]),
                    interpolated=math.isnan(levels[row, col]) and counts[row, col] == 0
                    and bool(buckets),
                    sample_count=int(counts[row, col]),
                )
            )

    finite = [c.level_db for c in cells if not math.isnan(c.level_db)]
    return CoverageMap(
        cells=cells,
        rows=rows,
        cols=cols,
        start_latitude=south,
        start_longitude=west,
        step_latitude=step_lat,
        step_longitude=step_lon,
        min_level_db=min(finite) if finite else float("nan"),
        max_level_db=max(finite) if finite else float("nan"),
    )


# --------------------------------------------------------------------------
# Амплитудная пеленгация
# --------------------------------------------------------------------------

@dataclass
class FixResult:
    """Результат триангуляции по пеленгам."""

    latitude: float
    longitude: float
    residual_deg: float
    bearings_used: int
    converged: bool

    def as_dict(self) -> dict:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "residual_deg": self.residual_deg,
            "bearings_used": self.bearings_used,
            "converged": self.converged,
        }


def triangulate_bearings(bearings: Sequence[Bearing]) -> Optional[FixResult]:
    """Определение координат источника по пересечению пеленгов.

    Пеленги задаются магнитными азимутами; истинные азимуты вычисляются с
    учётом магнитного склонения. Для нахождения точки используется
    наименьшие квадраты: минимизируется сумма квадратов отклонений
    азимутов от наблюдаемых.

    Требуется не менее трёх пеленгов (§5.2.1).
    """
    if len(bearings) < 3:
        return None

    lat0 = sum(b.latitude for b in bearings) / len(bearings)
    lon0 = sum(b.longitude for b in bearings) / len(bearings)

    # Перевод в локальную плоскость, м.
    lat_scale = math.pi * EARTH_RADIUS_M / 180.0
    lon_scale = lat_scale * math.cos(math.radians(lat0))

    def to_local(bearing: Bearing) -> tuple[float, float]:
        x = (bearing.longitude - lon0) * lon_scale
        y = (bearing.latitude - lat0) * lat_scale
        return x, y

    # Начальное приближение — пересечение первых двух пеленгов методом
    # детерминантов.
    b0, b1 = bearings[0], bearings[1]
    x0, y0 = to_local(b0)
    x1, y1 = to_local(b1)
    a0 = math.radians(b0.true_azimuth_deg)
    a1 = math.radians(b1.true_azimuth_deg)
    det = math.sin(a1 - a0)
    if abs(det) < 1e-12:
        return None
    px = (x0 * math.sin(a1) - x1 * math.sin(a0)) / det
    py = (-x0 * math.cos(a1) + x1 * math.cos(a0)) / det

    # Итеративное уточнение по всем пеленгам.
    for _ in range(50):
        grad_x = grad_y = 0.0
        hess_xx = hess_xy = hess_yy = 0.0
        residual_sum = 0.0
        for bearing in bearings:
            bx, by = to_local(bearing)
            dx = px - bx
            dy = py - by
            distance = math.hypot(dx, dy)
            if distance < 1e-6:
                continue
            azimuth = math.atan2(dx, dy)
            diff = (azimuth - math.radians(bearing.true_azimuth_deg) + math.pi) % (
                2 * math.pi
            ) - math.pi
            residual_sum += diff * diff
            # Производные азимута по координатам.
            d_az_dx = dy / (distance * distance)
            d_az_dy = -dx / (distance * distance)
            grad_x += diff * d_az_dx
            grad_y += diff * d_az_dy
            hess_xx += d_az_dx * d_az_dx
            hess_xy += d_az_dx * d_az_dy
            hess_yy += d_az_dy * d_az_dy

        det_h = hess_xx * hess_yy - hess_xy * hess_xy
        if abs(det_h) < 1e-18:
            break
        dx_step = -(hess_yy * grad_x - hess_xy * grad_y) / det_h
        dy_step = -(-hess_xy * grad_x + hess_xx * grad_y) / det_h
        px += dx_step
        py += dy_step
        if math.hypot(dx_step, dy_step) < 1e-3:
            break

    latitude = lat0 + py / lat_scale
    longitude = lon0 + px / lon_scale

    # Остаточное отклонение пеленгов от расчётных.
    residuals: list[float] = []
    for bearing in bearings:
        bx, by = to_local(bearing)
        dx = px - bx
        dy = py - by
        azimuth = math.atan2(dx, dy)
        diff = (azimuth - math.radians(bearing.true_azimuth_deg) + math.pi) % (
            2 * math.pi
        ) - math.pi
        residuals.append(abs(math.degrees(diff)))

    return FixResult(
        latitude=latitude,
        longitude=longitude,
        residual_deg=max(residuals) if residuals else float("nan"),
        bearings_used=len(bearings),
        converged=True,
    )


def select_bearing_points(
    points: Sequence[MeasurementPoint], count: int = 3
) -> list[MeasurementPoint]:
    """Выбор точек пеленгации, максимально удалённых друг от друга.

    Пеленги снимаются из 3 и более точек, расположенных на максимально
    возможном удалении друг от друга (§5.2.1).
    """
    if len(points) <= count:
        return list(points)
    chosen = [points[0]]
    remaining = list(points[1:])
    while len(chosen) < count and remaining:
        best = None
        best_distance = -1.0
        for candidate in remaining:
            distance = min(
                haversine_m(candidate.latitude, candidate.longitude, c.latitude, c.longitude)
                for c in chosen
            )
            if distance > best_distance:
                best_distance = distance
                best = candidate
        if best is None:
            break
        chosen.append(best)
        remaining.remove(best)
    return chosen


# ---------------------------------------------------------------------------
# Радиовидимость и антенный компас
# ---------------------------------------------------------------------------

#: Коэффициент радиогоризонта при нормальной атмосферной рефракции.
#: Соответствует эффективному радиусу Земли 4/3 (k ≈ 1,33): R ≈ 4,12·(√H + √h) км.
RADIO_HORIZON_COEFFICIENT = 4.12

#: Коэффициент геометрического горизонта без учёта рефракции (k = 1).
GEOMETRIC_HORIZON_COEFFICIENT = 3.57


def radio_horizon_km(
    transmitter_height_m: float,
    receiver_height_m: float,
    refraction: bool = True,
) -> float:
    """Дальность прямой радиовидимости между двумя антеннами, км.

    Формула радиовидимости исходного комплекса:
    ``Rм = 4,12 · (H^0,5 + h^0,5)``, где ``Н`` — высота передающей антенны,
    ``h`` — высота приёмной антенны (в метрах).

    Коэффициент 4,12 соответствует нормальной атмосферной рефракции
    (эффективный радиус Земли 4/3). Без учёта рефракции коэффициент
    равен 3,57 — это геометрический горизонт.
    """
    coefficient = RADIO_HORIZON_COEFFICIENT if refraction else GEOMETRIC_HORIZON_COEFFICIENT
    return coefficient * (
        math.sqrt(max(0.0, transmitter_height_m))
        + math.sqrt(max(0.0, receiver_height_m))
    )


def link_budget_range_km(
    frequency_hz: float,
    transmitter_power_kw: float,
    transmitter_gain_db: float = 0.0,
    receiver_gain_db: float = 0.0,
    receiver_sensitivity_dbm: float = -100.0,
) -> float:
    """Дальность прямой связи по бюджу линии, км.

    Параметры мощности и усиления задаются так же, как в географическом
    фильтре исходного комплекса: мощность передатчика — в дБкВт, коэффициент
    усиления передающей антенны — в дБ.
    """
    if frequency_hz <= 0 or transmitter_power_kw <= 0:
        return 0.0
    power_dbm = 10.0 * math.log10(transmitter_power_kw) + 30.0
    frequency_mhz = frequency_hz / 1e6
    budget = (
        power_dbm
        + transmitter_gain_db
        + receiver_gain_db
        - receiver_sensitivity_dbm
        - 32.44
        - 20.0 * math.log10(frequency_mhz)
    )
    if budget <= 0:
        return 0.0
    return 10.0 ** (budget / 20.0)


def visibility_radius_km(
    frequency_hz: float,
    transmitter_height_m: float,
    receiver_height_m: float,
    transmitter_power_kw: float,
    transmitter_gain_db: float = 0.0,
    receiver_sensitivity_dbuv_per_m: float = 0.0,
    receiver_gain_db: float = 0.0,
    receiver_impedance_ohm: float = 50.0,
    refraction: bool = True,
) -> float:
    """Дальность, на которой приёмник с заданной чувствительностью примет сигнал.

    Расчёт ограничивается двумя факторами: радиовидимостью (кривизна Земли)
    и затуханием сигнала в свободном пространстве. Исходный комплекс
    использует эту величину для географического фильтра и для построения
    лучей на карте: «прорисовываются лучи равные расстоянию, на которое
    работает РЭС с учётом параметров приёмника».
    """
    horizon = radio_horizon_km(transmitter_height_m, receiver_height_m, refraction)

    # Чувствительность в дБмкВ/м переводится в дБм через сопротивление.
    sensitivity_dbm = receiver_sensitivity_dbuv_per_m - 107.0 + 10.0 * math.log10(
        max(1e-9, receiver_impedance_ohm)
    )
    budget = link_budget_range_km(
        frequency_hz,
        transmitter_power_kw,
        transmitter_gain_db,
        receiver_gain_db,
        sensitivity_dbm,
    )
    if budget <= 0:
        return horizon
    return min(horizon, budget)


@dataclass
class AntennaCompassResult:
    """Результат обработки круговой диаграммы, снятой антенным компасом.

    Антенный компас исходного комплекса вращает направленную антенну на
    360° и одновременно измеряет уровень сигнала. Результаты отображаются
    на карте ломаной линией вокруг точки измерения, расстояние от точки до
    узлов которой пропорционально уровню сигнала; направление на источник
    выводится по минимумам и максимумам полученной фигуры.
    """

    azimuths_deg: list[float]
    levels_db: list[float]
    max_azimuth_deg: float
    max_level_db: float
    null_azimuths_deg: list[float]
    front_back_ratio_db: float = float("nan")
    warnings: list[str] = field(default_factory=list)

    def candidate_bearings_deg(self) -> list[float]:
        """Возможные направления на источник, градусы.

        Для направленной антенны направление совпадает с азимутом максимума.
        Для ненаправленной антенны направление определяется по минимуму
        диаграммы и имеет две неоднозначные составляющие, отстоящие от него
        на 90°.
        """
        if not self.null_azimuths_deg:
            return [self.max_azimuth_deg % 360.0]
        result: list[float] = []
        for null in self.null_azimuths_deg:
            for offset in (-90.0, 90.0):
                value = (null + offset) % 360.0
                if all(abs(value - item) > 1e-6 for item in result):
                    result.append(value)
        return result

    def as_dict(self) -> dict:
        return {
            "azimuths_deg": list(self.azimuths_deg),
            "levels_db": list(self.levels_db),
            "max_azimuth_deg": self.max_azimuth_deg,
            "max_level_db": self.max_level_db,
            "null_azimuths_deg": list(self.null_azimuths_deg),
            "front_back_ratio_db": self.front_back_ratio_db,
            "candidate_bearings_deg": self.candidate_bearings_deg(),
            "warnings": list(self.warnings),
        }


def antenna_compass(
    azimuths_deg: Sequence[float],
    levels_db: Sequence[float],
    tolerance_db: float = 1.0,
) -> AntennaCompassResult:
    """Обработка круговой диаграммы, снятой антенным компасом.

    Параметры
    ----------
    azimuths_deg:
        Азимуты, под которыми измерен уровень сигнала, градусы.
    levels_db:
        Уровни сигнала, соответствующие азимутам, дБ.
    tolerance_db:
        Допуск, в пределах которого отсчёты считаются относящимися к одному
        экстремуму диаграммы.
    """
    azimuths = [float(value) % 360.0 for value in azimuths_deg]
    levels = [float(value) for value in levels_db]
    warnings: list[str] = []

    if len(azimuths) < 3 or len(azimuths) != len(levels):
        raise ValueError(
            "Для обработки антенного компаса необходимо не менее трёх отсчётов "
            "с одинаковой длиной списков азимутов и уровней"
        )

    max_index = int(np.argmax(levels))
    max_azimuth = azimuths[max_index]
    max_level = levels[max_index]

    nulls: list[float] = []
    for index in range(len(levels)):
        if levels[index] <= min(levels) + tolerance_db:
            nulls.append(azimuths[index])

    # Сжатие соседних отсчётов в группы.
    grouped: list[float] = []
    for value in sorted(nulls):
        if not grouped or value - grouped[-1] > tolerance_db * 2.0:
            grouped.append(value)
    nulls = grouped

    opposite = (max_azimuth + 180.0) % 360.0
    back_level = min(
        (level for azimuth, level in zip(azimuths, levels)
         if abs(((azimuth - opposite + 180.0) % 360.0) - 180.0) <= 15.0),
        default=float("nan"),
    )
    front_back = max_level - back_level if np.isfinite(back_level) else float("nan")

    if not nulls:
        warnings.append(
            "Минимум диаграммы не выявлен — направление на источник определено "
            "только по максимуму"
        )

    return AntennaCompassResult(
        azimuths_deg=azimuths,
        levels_db=levels,
        max_azimuth_deg=max_azimuth,
        max_level_db=max_level,
        null_azimuths_deg=nulls,
        front_back_ratio_db=front_back,
        warnings=warnings,
    )
