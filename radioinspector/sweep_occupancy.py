"""Sample occupancy and acquisition-window coverage, never inferred RF airtime."""

from __future__ import annotations

import math
from typing import Any

from .contracts import parse_time
from .sweep_context import validate_context_request


def validate_occupancy_request(payload: Any) -> tuple[dict, float]:
    if not isinstance(payload, dict) or set(payload) != {"sweep_id", "before", "after", "threshold_dbm"}:
        raise ValueError("Нужны только sweep_id, before, after и threshold_dbm")
    threshold = payload["threshold_dbm"]
    try:
        valid = type(threshold) in (int, float) and math.isfinite(threshold)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("threshold_dbm должен быть конечным числом dBm")
    context_request = {key: payload[key] for key in ("sweep_id", "before", "after")}
    validate_context_request(context_request)
    return context_request, float(threshold)


def summarize_occupancy(context: dict, threshold_dbm: float) -> dict:
    """Consume a validated, detached sweep-context snapshot from the core.

    Each recorded bin has one vote per sweep, regardless of scan duration.
    Union of whole-scan intervals describes archive coverage only: individual
    frequency dwell times are unknown, so it is not continuous RF observation.
    """
    items = context["items"]
    samples = len(items)
    counts = [sum(item["levels"][index] > threshold_dbm for item in items)
              for index in range(context["points"])]
    intervals = sorted((parse_time(item["interval"]["started_at"]),
                        parse_time(item["interval"]["ended_at"])) for item in items)
    start, end = intervals[0]
    first = start
    covered = 0.0
    gaps = 0
    for next_start, next_end in intervals[1:]:
        if next_start > end:
            covered += (end - start).total_seconds()
            gaps += 1
            start, end = next_start, next_end
        else:
            end = max(end, next_end)
    covered += (end - start).total_seconds()
    span = (end - first).total_seconds()
    return {
        "method": "sample_fraction", "comparison": "strictly_greater",
        "threshold_dbm": threshold_dbm, "sample_count": samples,
        "exceedance_counts": counts,
        "sample_fractions": [count / samples for count in counts],
        "acquisition_span_seconds": span,
        "acquisition_covered_seconds": covered,
        "acquisition_gap_seconds": max(0.0, span - covered),
        "acquisition_gap_count": gaps,
        "rf_time_occupancy": None,
    }
