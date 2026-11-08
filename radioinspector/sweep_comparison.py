"""Read-only binwise comparison of recorded dBm sweeps, never a calibration.

Compatibility concerns the settings actually recorded in the archive. Equal
metadata cannot prove equal unrecorded settings, antennas or hardware state.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from .contracts import Sweep, to_primitive

MAX_COMPARISON_POINTS = 16_384


class ComparisonIncompatibleError(ValueError):
    """The archived measurements cannot be compared without assumptions."""


def _setting_value(value: Any, *, allow_null: bool = False) -> Any:
    """Build a type-aware JSON comparison key; null means unknown, not equal."""
    if value is None and allow_null:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ComparisonIncompatibleError("Настройки содержат неконечное число")
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, (list, tuple)):
        return ("array", tuple(_setting_value(item, allow_null=allow_null) for item in value))
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return ("object", tuple(sorted(
            (key, _setting_value(item, allow_null=allow_null)) for key, item in value.items()
        )))
    raise ComparisonIncompatibleError("Настройки содержат неизвестное или неподдерживаемое значение")


def _settings_key(settings: dict[str, Any]) -> Any:
    # sequence is a per-sweep counter, not an acquisition setting. No other
    # field, including an unknown extension, is silently discarded.
    if "sequence" in settings:
        # Ignoring the counter for compatibility must not let an invalid JSON
        # value escape into the response (memory-only ingest is permissive).
        _setting_value(settings["sequence"], allow_null=True)
    recorded = {key: value for key, value in settings.items() if key != "sequence"}
    if not recorded:
        raise ComparisonIncompatibleError("В свипе не записаны настройки измерения")
    return _setting_value(recorded)


def compare_sweeps(session_id: str, reference: Sweep, target: Sweep) -> dict[str, Any]:
    """Return target minus reference in dB, preserving every original bin.

    The maximum/minimum are signed extrema, even when all deltas are negative
    or positive. Ties choose the first bin in archived order. No interpolation,
    unit conversion, archive writes, device commands or events occur here.
    """
    if reference.session_id != session_id or target.session_id != session_id:
        raise ComparisonIncompatibleError("Оба свипа должны принадлежать выбранному сеансу")
    if reference.stream_id != target.stream_id:
        raise ComparisonIncompatibleError("Свипы должны принадлежать одному потоку")
    if reference.level_unit != "dBm" or target.level_unit != "dBm":
        raise ComparisonIncompatibleError("Сравнение поддерживает только исходные уровни dBm")
    if reference.frequencies_hz != target.frequencies_hz:
        raise ComparisonIncompatibleError("Частотные сетки свипов должны совпадать без интерполяции")
    points = len(reference.frequencies_hz)
    if points > MAX_COMPARISON_POINTS:
        raise ComparisonIncompatibleError(
            f"Сравнение поддерживает не более {MAX_COMPARISON_POINTS} точек без усечения"
        )
    if _settings_key(reference.settings) != _settings_key(target.settings):
        raise ComparisonIncompatibleError("Записанные настройки измерения свипов различаются")
    deltas = [measured - baseline for baseline, measured in zip(reference.levels, target.levels)]
    if any(not math.isfinite(value) for value in deltas):
        raise ComparisonIncompatibleError("Разность уровней выходит за конечный числовой диапазон")
    maximum = max(range(points), key=deltas.__getitem__)
    minimum = min(range(points), key=deltas.__getitem__)

    def measurement(sweep: Sweep) -> dict[str, Any]:
        return {
            "id": sweep.id,
            "interval": to_primitive(sweep.interval),
            "levels": list(sweep.levels),
            "level_unit": sweep.level_unit,
            "settings": copy.deepcopy(sweep.settings),
        }

    return {
        "session_id": session_id,
        "stream_id": reference.stream_id,
        "reference": measurement(reference),
        "target": measurement(target),
        "frequencies_hz": list(reference.frequencies_hz),
        "delta_levels": deltas,
        "delta_unit": "dB",
        "max_increase": {"frequency_hz": reference.frequencies_hz[maximum], "delta_db": deltas[maximum]},
        "min_change": {"frequency_hz": reference.frequencies_hz[minimum], "delta_db": deltas[minimum]},
        "points": points,
    }
