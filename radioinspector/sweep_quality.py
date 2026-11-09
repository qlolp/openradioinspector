"""Read-only review hints for archived dBm sweeps, not an RF diagnosis.

Never filter samples, change acquisition settings, stop a worker or attest
calibration. Thresholds are explicit user choices, not device safety limits.
"""
from __future__ import annotations

import copy
import json
import math
from typing import Any

from .contracts import Sweep, ensure_utc, to_primitive

MAX_QUALITY_REQUEST_BYTES = 8192
MAX_QUALITY_POINTS = 16_384
MAX_FREQUENCY_HZ = 9_007_199_254_740_991
METHOD = "archived_edge_level_review_v1"


class QualityIncompatibleError(ValueError):
    """The source cannot support this bounded derived review."""


def validate_quality_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"sweep_id", "edge_margin_db", "high_level_dbm"}:
        raise ValueError("Разрешены только sweep_id, edge_margin_db и high_level_dbm")
    sweep_id = payload["sweep_id"]
    if not isinstance(sweep_id, str) or not sweep_id.strip() or len(sweep_id) > 128:
        raise ValueError("sweep_id должен быть непустой строкой не длиннее 128 символов")
    try:
        sweep_id.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("sweep_id должен быть корректной строкой UTF-8") from exc
    for field, lower, upper in (("edge_margin_db", 0, 200), ("high_level_dbm", -200, 100)):
        value = payload[field]
        if (type(value) not in (int, float) or not lower <= value <= upper
                or not math.isfinite(value) or (field == "edge_margin_db" and value == 0)):
            raise ValueError(f"Некорректный {field}: требуется конечное число в допустимом диапазоне")
    return dict(payload)


def _validate_json_metadata(value: Any) -> None:
    """Reject non-JSON or nonrepresentable values; never normalize provenance."""
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise ValueError("Метаданные содержат непредставимое число")
        return
    if type(value) is list:
        for item in value:
            _validate_json_metadata(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for key, item in value.items():
            _validate_json_metadata(key)
            _validate_json_metadata(item)
        return
    raise ValueError("Метаданные должны содержать только JSON значения")


def calculate_sweep_quality(
    session_id: str, sweep: Sweep, *, edge_margin_db: float, high_level_dbm: float,
) -> dict[str, Any]:
    """Compare both endpoint bins to their immediate neighbours, without edits."""
    try:
        validate_quality_request(dict(sweep_id=sweep.id, edge_margin_db=edge_margin_db,
                                      high_level_dbm=high_level_dbm))
        if sweep.session_id != session_id or sweep.level_unit != "dBm":
            raise ValueError("Требуется исходный dBm sweep выбранного сеанса")
        for scope in (session_id, sweep.stream_id):
            if not isinstance(scope, str) or not scope.strip():
                raise ValueError("Некорректный идентификатор сеанса/потока")
            scope.encode("utf-8", errors="strict")
        frequencies, levels = sweep.frequencies_hz, sweep.levels
        points = len(frequencies)
        if not 2 <= points <= MAX_QUALITY_POINTS or len(levels) != points:
            raise ValueError("Требуется 2–16384 точки с совпадающими частотами и уровнями")
        if any(type(hz) is not int or not 0 <= hz <= MAX_FREQUENCY_HZ for hz in frequencies):
            raise ValueError("Частоты должны быть целыми безопасными Гц")
        if any(b <= a for a, b in zip(frequencies, frequencies[1:])):
            raise ValueError("Частотная сетка должна строго возрастать")
        if any(type(level) not in (int, float) or not math.isfinite(level) for level in levels):
            raise ValueError("Уровни должны быть конечными числами dBm")
        if ensure_utc(sweep.interval.ended_at) < ensure_utc(sweep.interval.started_at):
            raise ValueError("Неверный интервал измерения")
        _validate_json_metadata(sweep.settings)
        provenance = {"id": sweep.id, "interval": to_primitive(sweep.interval),
                      "level_unit": sweep.level_unit, "settings": copy.deepcopy(sweep.settings)}
        if len(json.dumps(provenance, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 65_536:
            raise ValueError("Метаданные исходного sweep превышают 64 KiB")
        edges = []
        flags = []
        for edge, index, neighbor in (("first", 0, 1), ("last", points - 1, points - 2)):
            delta = levels[index] - levels[neighbor]
            if not math.isfinite(delta):
                raise ValueError("Разность уровней не представима конечным числом")
            flagged = delta >= edge_margin_db
            edges.append({"edge": edge, "index": index, "frequency_hz": frequencies[index],
                          "level_dbm": levels[index], "neighbor_index": neighbor,
                          "neighbor_level_dbm": levels[neighbor], "delta_db": delta,
                          "flagged": flagged})
            if flagged:
                flags.append(edge + "_edge_jump")
        high_count = sum(level >= high_level_dbm for level in levels)
        if high_count:
            flags.append("level_threshold")
        peak = max(range(points), key=lambda index: levels[index])  # First peak on ties.
        return {
            "session_id": session_id, "stream_id": sweep.stream_id, "sweep": provenance,
            "parameters": {"edge_margin_db": edge_margin_db, "high_level_dbm": high_level_dbm},
            "points": points, "peak_index": peak, "peak_frequency_hz": frequencies[peak],
            "peak_level_dbm": levels[peak], "high_level_count": high_count,
            "edge_assessments": edges, "flags": flags,
            "assessment": "review" if flags else "no_flags", "method": METHOD,
            "hardware_diagnosis": False, "raw_modified": False,
        }
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise QualityIncompatibleError(str(exc)) from exc
