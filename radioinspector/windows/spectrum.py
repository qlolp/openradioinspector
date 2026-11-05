"""Чистые функции отображения исходных dBm sweeps в Windows GUI."""

from __future__ import annotations

from typing import Any, Optional

MAX_POINTS = 1000
MAX_SWEEPS = 40

_COLOR_STOPS = (
    (0.0, (5, 15, 28)),
    (0.25, (16, 65, 95)),
    (0.5, (31, 170, 184)),
    (0.72, (93, 225, 174)),
    (0.88, (255, 211, 110)),
    (1.0, (244, 100, 91)),
)


def sampled_indices(
    length: int, peak_index: int = -1, limit: int = MAX_POINTS
) -> list[int]:
    """Выбирает не более limit точек, обязательно сохраняя края и фактический пик."""
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise ValueError("Длина sweep должна быть неотрицательным целым")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 3:
        raise ValueError("Лимит точек должен быть целым не меньше 3")
    if length <= limit:
        return list(range(length))
    indices = {0, length - 1}
    if 0 <= peak_index < length:
        indices.add(peak_index)
    slots = limit - len(indices)
    for index in range(1, slots + 1):
        indices.add(round(index * (length - 1) / (slots + 1)))
    return sorted(indices)


def peak_of(sweep: dict[str, Any]) -> tuple[int, float, int]:
    """Возвращает индекс, исходный уровень и целую частоту максимума."""
    levels = sweep.get("levels") or []
    frequencies = sweep.get("frequencies_hz") or []
    if len(levels) != len(frequencies):
        raise ValueError("Частоты и уровни sweep имеют разную длину")
    if not levels:
        raise ValueError("Пустой sweep не имеет пика")
    index = max(range(len(levels)), key=lambda item: levels[item])
    return index, float(levels[index]), int(frequencies[index])


def waterfall_levels(
    sweeps: list[dict[str, Any]], columns: int
) -> tuple[list[list[float]], Optional[float], Optional[float]]:
    """Агрегирует каждый пиксельный столбец по максимуму без изменения единиц."""
    if isinstance(columns, bool) or not isinstance(columns, int) or columns < 1:
        raise ValueError("Число столбцов должно быть положительным целым")
    rows: list[list[float]] = []
    all_levels: list[float] = []
    for sweep in sweeps[-MAX_SWEEPS:]:
        levels = sweep.get("levels") or []
        frequencies = sweep.get("frequencies_hz") or []
        if len(levels) != len(frequencies):
            raise ValueError("Частоты и уровни sweep имеют разную длину")
        if not levels:
            continue
        all_levels.extend(float(level) for level in levels)
        row: list[float] = []
        for column in range(columns):
            start = column * len(levels) // columns
            end = max(start + 1, (column + 1) * len(levels) // columns)
            row.append(float(max(levels[start : min(end, len(levels))])))
        rows.append(row)
    if not all_levels:
        return [], None, None
    return rows, min(all_levels), max(all_levels)


def waterfall_color(level: float, minimum: float, maximum: float) -> str:
    """Возвращает цвет уровня; шкала остаётся числовой и не меняет исходные dBm."""
    span = max(float(maximum) - float(minimum), 1.0)
    ratio = max(0.0, min(1.0, (float(level) - float(minimum)) / span))
    upper = 1
    while upper < len(_COLOR_STOPS) - 1 and _COLOR_STOPS[upper][0] < ratio:
        upper += 1
    lower = upper - 1
    lower_position, lower_color = _COLOR_STOPS[lower]
    upper_position, upper_color = _COLOR_STOPS[upper]
    local = (ratio - lower_position) / (upper_position - lower_position)
    channels = [
        round(lower_color[index] + (upper_color[index] - lower_color[index]) * local)
        for index in range(3)
    ]
    return "#" + "".join(f"{channel:02x}" for channel in channels)
