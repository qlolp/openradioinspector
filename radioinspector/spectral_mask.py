"""Portable spectral masks and read-only evaluation at exact archived bins.

Only the mask is interpolated, linearly in dBm. Source measurements are never
interpolated, calibrated, changed, or stored by this module.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

from .contracts import Sweep, to_primitive
from .sweep_comparison import ComparisonIncompatibleError, compare_sweeps
from .sweep_references import normalize_reference_name
from .sweep_threshold import exceedance_regions

MASK_FORMAT = "openradioinspector-spectral-mask"
MAX_MASK_BYTES = 65_536
MAX_MASK_REQUEST_BYTES = 524_288
MAX_FREQUENCY_HZ = 9_007_199_254_740_991
MAX_MASK_VERTICES = 128


class MaskIncompatibleError(ValueError):
    """The source, coverage or derived values cannot support mask analysis."""


def strict_json_loads(text: str) -> Any:
    """Reject duplicate fields, non-JSON numbers and lone Unicode surrogates."""
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Поля JSON не должны повторяться")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("JSON не должен содержать NaN или Infinity")

    def check_strings(value):
        if isinstance(value, str):
            value.encode("utf-8", errors="strict")
        elif isinstance(value, list):
            for item in value:
                check_strings(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                check_strings(key)
                check_strings(item)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON не должен содержать неконечные числа")

    try:
        text.encode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=unique_fields, parse_constant=invalid_constant)
        check_strings(value)
        return value
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("Некорректный JSON UTF-8") from exc


def parse_spectral_mask(mask_json: str) -> dict[str, Any]:
    """Validate an imported raw document and return its canonical artifact."""
    if not isinstance(mask_json, str):
        raise ValueError("mask_json должен быть строкой JSON UTF-8")
    try:
        size = len(mask_json.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError("Некорректный JSON UTF-8 маски") from exc
    if size > MAX_MASK_BYTES:
        raise ValueError(f"Маска должна занимать не более {MAX_MASK_BYTES} байт UTF-8")
    mask = strict_json_loads(mask_json)
    fields = {"format", "format_version", "name", "frequency_unit", "level_unit", "interpolation", "vertices"}
    if not isinstance(mask, dict) or set(mask) != fields:
        raise ValueError("Некорректные поля спектральной маски")
    if (mask["format"] != MASK_FORMAT or type(mask["format_version"]) is not int
            or mask["format_version"] != 1):
        raise ValueError("Неподдерживаемый формат или версия спектральной маски")
    if (mask["frequency_unit"] != "Hz" or mask["level_unit"] != "dBm"
            or mask["interpolation"] != "linear_dbm"):
        raise ValueError("Маска требует Hz, dBm и linear_dbm")
    name = normalize_reference_name(mask["name"])
    vertices = mask["vertices"]
    if not isinstance(vertices, list) or not 2 <= len(vertices) <= MAX_MASK_VERTICES:
        raise ValueError(f"Маска должна содержать от 2 до {MAX_MASK_VERTICES} вершин")
    canonical_vertices = []
    previous = -1
    for vertex in vertices:
        if not isinstance(vertex, dict) or set(vertex) != {"frequency_hz", "limit_dbm"}:
            raise ValueError("Вершина требует только frequency_hz и limit_dbm")
        frequency = vertex["frequency_hz"]
        if type(frequency) is not int or not previous < frequency <= MAX_FREQUENCY_HZ:
            raise ValueError("Частоты вершин должны строго возрастать в диапазоне безопасных целых Hz")
        level = vertex["limit_dbm"]
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ValueError("limit_dbm должен быть конечным числом")
        try:
            level = float(level)
        except (ValueError, OverflowError) as exc:
            raise ValueError("limit_dbm должен быть конечным числом") from exc
        if not math.isfinite(level):
            raise ValueError("limit_dbm должен быть конечным числом")
        canonical_vertices.append({"frequency_hz": frequency, "limit_dbm": level})
        previous = frequency
    return {
        "format": MASK_FORMAT, "format_version": 1, "name": name,
        "frequency_unit": "Hz", "level_unit": "dBm", "interpolation": "linear_dbm",
        "vertices": canonical_vertices,
    }


def _interpolate(frequency: int, left: dict, right: dict) -> float:
    low, high = left["limit_dbm"], right["limit_dbm"]
    if frequency == left["frequency_hz"] or low == high:
        return low
    if frequency == right["frequency_hz"]:
        return high
    weight = (frequency - left["frequency_hz"]) / (right["frequency_hz"] - left["frequency_hz"])
    # The difference cannot overflow for same-sign endpoints. For opposite
    # signs use a convex weighted sum, never the overflowing high - low.
    if (low >= 0) == (high >= 0):
        return low + (high - low) * weight
    return low * (1 - weight) + high * weight


def calculate_sweep_mask(session_id: str, sweep: Sweep, *, mask_json: str) -> dict[str, Any]:
    mask = parse_spectral_mask(mask_json)
    try:
        compare_sweeps(session_id, sweep, sweep)
    except ComparisonIncompatibleError as exc:
        raise MaskIncompatibleError(str(exc)) from exc
    frequencies = sweep.frequencies_hz
    if any(type(f) is not int or not 0 <= f <= MAX_FREQUENCY_HZ for f in frequencies):
        raise MaskIncompatibleError("Частоты свипа выходят за диапазон безопасных целых Hz")
    vertices = mask["vertices"]
    if frequencies[0] < vertices[0]["frequency_hz"] or frequencies[-1] > vertices[-1]["frequency_hz"]:
        raise MaskIncompatibleError("Маска должна полностью покрывать частоты свипа без экстраполяции")
    threshold = []
    segment = 0
    for frequency in frequencies:
        while segment < len(vertices) - 2 and frequency > vertices[segment + 1]["frequency_hz"]:
            segment += 1
        threshold.append(_interpolate(frequency, vertices[segment], vertices[segment + 1]))
    excess = [level - boundary for level, boundary in zip(sweep.levels, threshold)]
    if any(not math.isfinite(value) for values in (threshold, excess) for value in values):
        raise MaskIncompatibleError("Производные уровни выходят за конечный числовой диапазон")
    regions = exceedance_regions(frequencies, excess)
    return {
        "session_id": session_id, "stream_id": sweep.stream_id,
        "sweep": {
            "id": sweep.id, "interval": to_primitive(sweep.interval),
            "levels": list(sweep.levels), "level_unit": sweep.level_unit,
            "settings": copy.deepcopy(sweep.settings),
        },
        "mask": mask, "frequencies_hz": list(frequencies),
        "threshold_levels": threshold, "level_unit": "dBm", "excess_levels": excess,
        "delta_unit": "dB", "comparison": "strictly_greater", "regions": regions,
        "points": len(frequencies), "exceedance_points": sum(r["points"] for r in regions),
        "region_count": len(regions),
    }
