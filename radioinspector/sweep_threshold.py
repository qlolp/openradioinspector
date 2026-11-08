"""Read-only centered rolling-median thresholds for recorded dBm sweeps.

The local median includes the measured bin and is a derived baseline, not a
calibration or a noise-floor measurement. Regions follow archived bin indices,
not inferred frequency gaps. Nothing here creates candidates or device work.
"""

from __future__ import annotations

import copy
import math
import sys
from typing import Any

from .contracts import Sweep, to_primitive
from .sweep_comparison import ComparisonIncompatibleError, compare_sweeps


class ThresholdIncompatibleError(ValueError):
    """The source or derived values cannot support this threshold analysis."""


def validate_threshold_parameters(window_points: int, margin_db: float) -> tuple[int, float]:
    """Validate typed parameters without silently coercing booleans or strings."""
    if (isinstance(window_points, bool) or not isinstance(window_points, int)
            or not 3 <= window_points <= 101 or window_points % 2 != 1):
        raise ValueError("window_points должен быть нечётным целым от 3 до 101")
    if isinstance(margin_db, bool) or not isinstance(margin_db, (int, float)):
        raise ValueError("margin_db должен быть конечным числом от 0 до 120 dB")
    try:
        margin = float(margin_db)
    except (OverflowError, ValueError) as exc:
        raise ValueError("margin_db должен быть конечным числом от 0 до 120 dB") from exc
    if not math.isfinite(margin) or not 0 <= margin <= 120:
        raise ValueError("margin_db должен быть конечным числом от 0 до 120 dB")
    return window_points, margin


def _median(values: tuple[float, ...]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    low, high = ordered[middle - 1:middle + 1]
    # Divide first only at overflow-sized magnitudes. For small values, add
    # first so equal subnormals survive and half-subnormal ties round normally.
    if max(abs(low), abs(high)) > sys.float_info.max / 2:
        return low / 2 + high / 2
    return (low + high) / 2


def calculate_sweep_threshold(
    session_id: str, sweep: Sweep, *, window_points: int, margin_db: float,
) -> dict[str, Any]:
    """Preserve every source bin and return signed excess over a local threshold.

    Windows are centered, include the center and clip at both ends. An even
    clipped window uses the mean of its middle pair. Equality never exceeds the
    threshold. Region endpoints are inclusive and peak ties use the first bin.
    """
    window_points, margin_db = validate_threshold_parameters(window_points, margin_db)
    try:
        # Share the established dBm/settings/finite/max-points gate unchanged.
        compare_sweeps(session_id, sweep, sweep)
    except ComparisonIncompatibleError as exc:
        raise ThresholdIncompatibleError(str(exc)) from exc
    points = len(sweep.frequencies_hz)
    if points < 3:
        raise ThresholdIncompatibleError("Для адаптивного порога нужны хотя бы 3 точки")
    radius = window_points // 2
    baseline = [
        _median(sweep.levels[max(0, index - radius):min(points, index + radius + 1)])
        for index in range(points)
    ]
    threshold = [level + margin_db for level in baseline]
    excess = [level - boundary for level, boundary in zip(sweep.levels, threshold)]
    if any(not math.isfinite(value) for values in (baseline, threshold, excess) for value in values):
        raise ThresholdIncompatibleError("Производные уровни выходят за конечный числовой диапазон")

    regions = []
    index = 0
    while index < points:
        if excess[index] <= 0:
            index += 1
            continue
        start = peak = index
        index += 1
        while index < points and excess[index] > 0:
            if excess[index] > excess[peak]:
                peak = index
            index += 1
        end = index - 1
        regions.append({
            "start_index": start, "end_index": end,
            "start_frequency_hz": sweep.frequencies_hz[start],
            "end_frequency_hz": sweep.frequencies_hz[end],
            "peak_index": peak, "peak_frequency_hz": sweep.frequencies_hz[peak],
            "peak_excess_db": excess[peak], "points": end - start + 1,
        })

    return {
        "session_id": session_id, "stream_id": sweep.stream_id,
        "sweep": {
            "id": sweep.id, "interval": to_primitive(sweep.interval),
            "levels": list(sweep.levels), "level_unit": sweep.level_unit,
            "settings": copy.deepcopy(sweep.settings),
        },
        "frequencies_hz": list(sweep.frequencies_hz),
        "parameters": {
            "method": "rolling_median", "window_points": window_points, "margin_db": margin_db,
            "edge_policy": "clip", "comparison": "strictly_greater",
        },
        "baseline_levels": baseline, "threshold_levels": threshold, "level_unit": "dBm",
        "excess_levels": excess, "delta_unit": "dB", "regions": regions, "points": points,
        "exceedance_points": sum(region["points"] for region in regions), "region_count": len(regions),
    }
