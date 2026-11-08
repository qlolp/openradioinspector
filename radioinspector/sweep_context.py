"""Read-only, bounded neighbours of an exact archived sweep.

Ordering is by UTC start time and exact UTF-8 ID, not interval non-overlap.
Select actual neighbours before checking compatibility: never skip a changed
configuration to manufacture a complete window.
"""

from __future__ import annotations

import copy
import heapq
import json
from typing import Any, Iterable

from .contracts import Sweep, ensure_utc, to_primitive
from .sweep_comparison import validate_sweep_compatibility

MAX_CONTEXT_REQUEST_BYTES = 8192
MAX_CONTEXT_NEIGHBOURS = 16
MAX_CONTEXT_TOTAL_POINTS = 131_072
MAX_CONTEXT_ID_LENGTH = 128
MAX_FREQUENCY_HZ = 9_007_199_254_740_991


class ContextIncompatibleError(ValueError):
    """Selected source data cannot form a compatible context window."""


def validate_context_request(payload: Any) -> tuple[str, int, int]:
    if not isinstance(payload, dict) or set(payload) != {"sweep_id", "before", "after"}:
        raise ValueError("Разрешены только поля sweep_id, before и after")
    sweep_id = payload["sweep_id"]
    if (not isinstance(sweep_id, str) or not sweep_id.strip()
            or len(sweep_id) > MAX_CONTEXT_ID_LENGTH):
        raise ValueError("ID свипа должен быть непустой строкой не длиннее 128 символов")
    try:
        sweep_id.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("ID свипа должен быть корректной строкой UTF-8") from exc
    for field in ("before", "after"):
        if type(payload[field]) is not int or not 0 <= payload[field] <= MAX_CONTEXT_NEIGHBOURS:
            raise ValueError(f"{field} должен быть целым числом от 0 до 16")
    return sweep_id, payload["before"], payload["after"]


def sweep_order_key(sweep: Sweep) -> tuple:
    """Match SQLite's normalized UTC timestamp and binary UTF-8 ID ordering."""
    try:
        if not isinstance(sweep.id, str):
            raise ValueError("ID исходного свипа должен быть строкой")
        return ensure_utc(sweep.interval.started_at), sweep.id.encode("utf-8", errors="strict")
    except (TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ContextIncompatibleError("Некорректное время или ID исходного свипа") from exc


def select_context_neighbours(
    anchor: Sweep, sweeps: Iterable[Sweep], *, before: int, after: int,
) -> tuple[list[Sweep], list[Sweep]]:
    """One archive pass and O(before + after) auxiliary memory, no deep copies."""
    anchor_key = sweep_order_key(anchor)
    if before == after == 0:
        return [], []
    preceding = []
    following = []
    for sweep in sweeps:
        if sweep.session_id != anchor.session_id or sweep.stream_id != anchor.stream_id:
            continue
        key = sweep_order_key(sweep)
        if key < anchor_key and before:
            # Keep the largest preceding keys in a bounded minimum heap.
            entry = (key, sweep)
            if len(preceding) < before:
                heapq.heappush(preceding, entry)
            elif key > preceding[0][0]:
                heapq.heapreplace(preceding, entry)
        elif key > anchor_key and after:
            # At most 16 following entries; bounded insertion avoids a custom
            # reverse wrapper for a datetime/bytes key.
            following.append((key, sweep))
            following.sort(key=lambda entry: entry[0])
            if len(following) > after:
                following.pop()
    preceding.sort(key=lambda entry: entry[0])
    return [item for _, item in preceding], [item for _, item in following]


def calculate_sweep_context(
    session_id: str, anchor: Sweep, preceding: list[Sweep], following: list[Sweep],
    *, before: int, after: int,
) -> dict[str, Any]:
    """Return a detached snapshot; later queries may differ after late imports."""
    selected = [*preceding, anchor, *following]
    try:
        points = len(anchor.frequencies_hz)
        if points * len(selected) > MAX_CONTEXT_TOTAL_POINTS:
            raise ContextIncompatibleError("Окно контекста превышает 131072 исходных точек")
        for sweep in selected:
            # Bound returned identities only after selecting actual neighbours;
            # an unrelated legacy long ID must not poison a different window.
            validate_context_request({"sweep_id": sweep.id, "before": 0, "after": 0})
            validate_sweep_compatibility(session_id, anchor, sweep)
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in sweep.levels):
                raise ContextIncompatibleError("Исходные уровни должны быть конечными числами")
            sweep_order_key(sweep)
            if ensure_utc(sweep.interval.ended_at) < ensure_utc(sweep.interval.started_at):
                raise ContextIncompatibleError("Некорректный интервал исходного свипа")
            if any(type(f) is not int or not 0 <= f <= MAX_FREQUENCY_HZ for f in sweep.frequencies_hz):
                raise ContextIncompatibleError("Частоты свипа выходят за диапазон безопасных целых Hz")
            if any(b < a for a, b in zip(sweep.frequencies_hz, sweep.frequencies_hz[1:])):
                raise ContextIncompatibleError("Частоты свипа должны идти в неубывающем порядке")
            # The shared gate rejects unknown settings. Also ensure recorded
            # string metadata can actually be returned as strict UTF-8 JSON.
            json.dumps(sweep.settings, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
    except (TypeError, ValueError, AttributeError, OverflowError, RecursionError) as exc:
        raise ContextIncompatibleError(str(exc)) from exc
    available = {"before": len(preceding), "after": len(following)}
    missing = {"before": before - len(preceding), "after": after - len(following)}
    return {
        "session_id": session_id, "stream_id": anchor.stream_id, "anchor_id": anchor.id,
        "order": "started_at_then_id", "requested": {"before": before, "after": after},
        "available": available, "missing": missing, "complete": not any(missing.values()),
        "frequencies_hz": list(anchor.frequencies_hz), "level_unit": "dBm", "points": points,
        "items": [{
            "offset": index - len(preceding), "id": sweep.id,
            "interval": to_primitive(sweep.interval), "levels": list(sweep.levels),
            "level_unit": sweep.level_unit, "settings": copy.deepcopy(sweep.settings),
        } for index, sweep in enumerate(selected)],
    }
