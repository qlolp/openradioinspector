"""Read-only, equal-weight mean power reference from exact archived dBm sweeps.

This is a derived baseline, never a new Sweep, calibration, or time sequence.
Only recorded acquisition settings establish compatibility.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Sequence

from .contracts import Sweep, to_primitive
from .sweep_comparison import (
    MAX_COMPARISON_POINTS, ComparisonIncompatibleError, validate_sweep_compatibility,
)

MAX_AVERAGE_REFERENCE_SWEEPS = 16
MAX_AVERAGE_REQUEST_BYTES = 8192
MAX_AVERAGE_ID_LENGTH = 128
MAX_FREQUENCY_HZ = 9_007_199_254_740_991
MAX_CHANGE_THRESHOLD_DB = 120.0


class AverageReferenceIncompatibleError(ValueError):
    """Sources or derived values cannot support an averaged reference."""


def validate_average_request(payload: Any) -> tuple[list[str], str]:
    if not isinstance(payload, dict) or set(payload) != {"reference_ids", "target_id"}:
        raise ValueError("Разрешены только поля reference_ids и target_id")
    references, target = payload["reference_ids"], payload["target_id"]
    if not isinstance(references, list) or not 2 <= len(references) <= MAX_AVERAGE_REFERENCE_SWEEPS:
        raise ValueError("reference_ids должен содержать от 2 до 16 ID свипов")
    for value in [*references, target]:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_AVERAGE_ID_LENGTH:
            raise ValueError("ID свипа должен быть непустой строкой не длиннее 128 символов")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("ID свипа должен быть корректной строкой UTF-8") from exc
    if len(set(references)) != len(references):
        raise ValueError("reference_ids не должны повторяться")
    if target in references:
        raise ValueError("target_id не должен входить в reference_ids")
    # Exact identity: whitespace and punctuation are never silently normalized.
    return list(references), target


def validate_change_detection_request(payload: Any) -> tuple[list[str], str, float, int]:
    if not isinstance(payload, dict) or set(payload) != {
        "reference_ids", "target_id", "minimum_increase_db", "minimum_bins",
    }:
        raise ValueError(
            "Разрешены только reference_ids, target_id, minimum_increase_db и minimum_bins"
        )
    reference_ids, target_id = validate_average_request({
        "reference_ids": payload["reference_ids"], "target_id": payload["target_id"],
    })
    threshold = payload["minimum_increase_db"]
    if (
        isinstance(threshold, bool) or not isinstance(threshold, (int, float))
        or not 0 <= threshold <= MAX_CHANGE_THRESHOLD_DB or not math.isfinite(threshold)
    ):
        raise ValueError("minimum_increase_db должен быть конечным числом от 0 до 120 dB")
    minimum_bins = payload["minimum_bins"]
    if (
        isinstance(minimum_bins, bool) or not isinstance(minimum_bins, int)
        or not 1 <= minimum_bins <= 16_384
    ):
        raise ValueError("minimum_bins должен быть целым числом от 1 до 16384")
    return reference_ids, target_id, float(threshold), minimum_bins


def _mean_power_dbm(values: Sequence[float]) -> float:
    peak = max(values)
    # Shift before exponentiation: exponents are never positive. A subtraction
    # of opposite extreme finite values can become -inf, whose power correctly
    # rounds to zero. No absolute linear power is ever constructed.
    scaled = math.fsum(math.exp((value - peak) * (math.log(10) / 10)) for value in values)
    mean = peak + 10 * math.log10(scaled / len(values))
    # Rounding exp/log around adjacent floating-point values can move the
    # result one ULP outside the source range. A physical equal-weight power
    # mean is bounded by its inputs, so retain that invariant for clients.
    return min(peak, max(min(values), mean))


def calculate_sweep_average_comparison(
    session_id: str, references: Sequence[Sweep], target: Sweep,
) -> dict[str, Any]:
    """Compare a separate target with an equal-weight mean in linear power.

    All archived bins and duplicate frequencies survive. Signed extrema choose
    the first bin on ties. Intervals are provenance only, not weighting or an
    assertion that the target chronologically follows the references.
    """
    source_ids, _ = validate_average_request({
        "reference_ids": [sweep.id for sweep in references], "target_id": target.id,
    })
    try:
        for sweep in references:
            validate_sweep_compatibility(session_id, sweep, target)
    except ComparisonIncompatibleError as exc:
        raise AverageReferenceIncompatibleError(str(exc)) from exc
    frequencies = target.frequencies_hz
    if any(type(f) is not int or not 0 <= f <= MAX_FREQUENCY_HZ for f in frequencies):
        raise AverageReferenceIncompatibleError("Частоты свипа выходят за диапазон безопасных целых Hz")
    baseline = [_mean_power_dbm(levels) for levels in zip(*(sweep.levels for sweep in references))]
    deltas = [level - mean for level, mean in zip(target.levels, baseline)]
    if any(not math.isfinite(value) for values in (baseline, deltas) for value in values):
        raise AverageReferenceIncompatibleError("Производные уровни выходят за конечный числовой диапазон")
    maximum = max(range(len(frequencies)), key=deltas.__getitem__)
    minimum = min(range(len(frequencies)), key=deltas.__getitem__)
    return {
        "session_id": session_id, "stream_id": target.stream_id,
        "frequencies_hz": list(frequencies),
        "reference": {
            "method": "mean_power", "count": len(references), "source_ids": source_ids,
            "sources": [{"id": sweep.id, "interval": to_primitive(sweep.interval),
                         "settings": copy.deepcopy(sweep.settings)} for sweep in references],
            "interval": {
                "started_at": to_primitive(min(sweep.interval.started_at for sweep in references)),
                "ended_at": to_primitive(max(sweep.interval.ended_at for sweep in references)),
            },
            "levels": baseline, "level_unit": "dBm",
        },
        "target": {
            "id": target.id, "interval": to_primitive(target.interval), "levels": list(target.levels),
            "level_unit": target.level_unit, "settings": copy.deepcopy(target.settings),
        },
        "delta_levels": deltas, "delta_unit": "dB",
        "max_increase": {"frequency_hz": frequencies[maximum], "delta_db": deltas[maximum]},
        "min_change": {"frequency_hz": frequencies[minimum], "delta_db": deltas[minimum]},
        "points": len(frequencies),
    }


def detect_change_regions(
    frequencies_hz: Sequence[int], delta_levels: Sequence[float], *,
    minimum_increase_db: float, minimum_bins: int,
) -> list[dict[str, Any]]:
    """Find contiguous regions meeting an explicit positive delta threshold."""
    regions: list[dict[str, Any]] = []
    start: int | None = None

    def append(end: int) -> None:
        if start is None or end - start + 1 < minimum_bins:
            return
        peak = max(range(start, end + 1), key=delta_levels.__getitem__)
        regions.append({
            "start_index": start,
            "end_index": end,
            "start_frequency_hz": frequencies_hz[start],
            "end_frequency_hz": frequencies_hz[end],
            "peak_index": peak,
            "peak_frequency_hz": frequencies_hz[peak],
            "peak_increase_db": delta_levels[peak],
            "bin_count": end - start + 1,
        })

    for index, value in enumerate(delta_levels):
        if value >= minimum_increase_db:
            if start is None:
                start = index
        elif start is not None:
            append(index - 1)
            start = None
    if start is not None:
        append(len(delta_levels) - 1)
    return regions


def calculate_sweep_average_change_detection(
    session_id: str, references: Sequence[Sweep], target: Sweep, *,
    minimum_increase_db: float, minimum_bins: int,
) -> dict[str, Any]:
    """Return a read-only average comparison and thresholded delta regions."""
    if (
        isinstance(minimum_increase_db, bool)
        or not isinstance(minimum_increase_db, (int, float))
        or not 0 <= minimum_increase_db <= MAX_CHANGE_THRESHOLD_DB
        or not math.isfinite(minimum_increase_db)
    ):
        raise ValueError("minimum_increase_db должен быть конечным числом от 0 до 120 dB")
    if (
        isinstance(minimum_bins, bool) or not isinstance(minimum_bins, int)
        or not 1 <= minimum_bins <= MAX_COMPARISON_POINTS
    ):
        raise ValueError("minimum_bins должен быть целым числом от 1 до 16384")
    comparison = calculate_sweep_average_comparison(session_id, references, target)
    regions = detect_change_regions(
        comparison["frequencies_hz"], comparison["delta_levels"],
        minimum_increase_db=float(minimum_increase_db), minimum_bins=minimum_bins,
    )
    return {
        "comparison": comparison,
        "detection": {
            "minimum_increase_db": float(minimum_increase_db),
            "minimum_bins": minimum_bins,
            "regions": regions,
            "count": len(regions),
        },
    }
