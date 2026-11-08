"""Three independent horizontal thresholds over one exact archived dBm sweep.

This opt-in analysis preserves source bins and never creates candidates, events,
archive rows, or device work. Counts belong to each line and may overlap.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from .contracts import Sweep, to_primitive
from .sweep_comparison import ComparisonIncompatibleError, validate_sweep_compatibility
from .sweep_threshold import exceedance_regions

MAX_THREE_THRESHOLDS_REQUEST_BYTES = 8192
MAX_THREE_THRESHOLDS_ID_LENGTH = 128
MAX_FREQUENCY_HZ = 9_007_199_254_740_991


class ThreeThresholdsIncompatibleError(ValueError):
    """The source or any line's derived values cannot support this analysis."""


def validate_three_thresholds_request(payload: Any) -> tuple[str, list[float]]:
    """Require one exact identity and three finite JSON numbers in input order."""
    if not isinstance(payload, dict) or set(payload) != {"sweep_id", "levels_dbm"}:
        raise ValueError("Разрешены только поля sweep_id и levels_dbm")
    sweep_id = payload["sweep_id"]
    if (not isinstance(sweep_id, str) or not sweep_id.strip()
            or len(sweep_id) > MAX_THREE_THRESHOLDS_ID_LENGTH):
        raise ValueError("ID свипа должен быть непустой строкой не длиннее 128 символов")
    try:
        sweep_id.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("ID свипа должен быть корректной строкой UTF-8") from exc
    levels = payload["levels_dbm"]
    if not isinstance(levels, list) or len(levels) != 3:
        raise ValueError("levels_dbm должен содержать ровно три конечных числа dBm")
    canonical_levels = []
    for level in levels:
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ValueError("Каждый уровень порога должен быть конечным числом dBm")
        try:
            level = float(level)
        except (ValueError, OverflowError) as exc:
            raise ValueError("Каждый уровень порога должен быть конечным числом dBm") from exc
        if not math.isfinite(level):
            raise ValueError("Каждый уровень порога должен быть конечным числом dBm")
        canonical_levels.append(level)
    # Neither source identity nor equal/arbitrarily ordered levels are normalized.
    return sweep_id, canonical_levels


def calculate_sweep_three_thresholds(
    session_id: str, sweep: Sweep, *, levels_dbm: list[float],
) -> dict[str, Any]:
    """Return signed source-minus-line excess with strict positive index runs.

Line IDs follow input order. Region endpoints are inclusive, peak ties choose
the first bin, and one overflowing line invalidates the entire analysis.
"""
    _, levels_dbm = validate_three_thresholds_request({"sweep_id": sweep.id, "levels_dbm": levels_dbm})
    try:
        validate_sweep_compatibility(session_id, sweep, sweep)
    except ComparisonIncompatibleError as exc:
        raise ThreeThresholdsIncompatibleError(str(exc)) from exc
    frequencies = sweep.frequencies_hz
    if any(type(f) is not int or not 0 <= f <= MAX_FREQUENCY_HZ for f in frequencies):
        raise ThreeThresholdsIncompatibleError("Частоты свипа выходят за диапазон безопасных целых Hz")
    lines = []
    for index, level in enumerate(levels_dbm, start=1):
        excess = [source - level for source in sweep.levels]
        if any(not math.isfinite(value) for value in excess):
            raise ThreeThresholdsIncompatibleError("Производные уровни выходят за конечный числовой диапазон")
        regions = exceedance_regions(frequencies, excess)
        lines.append({
            "id": f"line-{index}", "level_dbm": level, "excess_levels": excess,
            "regions": regions, "exceedance_points": sum(region["points"] for region in regions),
            "region_count": len(regions),
        })
    return {
        "session_id": session_id, "stream_id": sweep.stream_id,
        "sweep": {
            "id": sweep.id, "interval": to_primitive(sweep.interval),
            "levels": list(sweep.levels), "level_unit": sweep.level_unit,
            "settings": copy.deepcopy(sweep.settings),
        },
        "frequencies_hz": list(frequencies), "method": "three_horizontal",
        "comparison": "strictly_greater", "level_unit": "dBm", "delta_unit": "dB",
        "points": len(frequencies), "lines": lines,
    }
