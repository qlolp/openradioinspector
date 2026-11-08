"""Portable integer frequency grids and read-only candidate derivation.

Only the derived result is rounded. Candidates, handoff targets, original
levels and their units are never changed or persisted by this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .contracts import Candidate, TimeInterval, to_primitive
from .spectral_mask import strict_json_loads
from .sweep_references import normalize_reference_name

ROUNDING_FORMAT = "openradioinspector-frequency-rounding"
MAX_PROFILE_BYTES = 65_536
MAX_ROUNDING_REQUEST_BYTES = 524_288
MAX_FREQUENCY_HZ = 9_007_199_254_740_991
MAX_ROUNDING_RULES = 128
MAX_CANDIDATE_ID_LENGTH = 128
RULE_FIELDS = frozenset({
    "start_frequency_hz", "end_frequency_hz", "step_hz", "origin_frequency_hz", "max_offset_hz",
})


class FrequencyRoundingIncompatibleError(ValueError):
    """The archived candidate cannot safely support a derived frequency."""


def _text(value: Any, field: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} должен быть непустой строкой")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field} должен содержать не более {maximum} символов")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"{field} должен быть корректной строкой UTF-8") from exc
    return value


def validate_rounding_request(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict) or set(payload) != {"candidate_id", "profile_json"}:
        raise ValueError("Разрешены только поля candidate_id и profile_json")
    candidate_id = _text(payload["candidate_id"], "candidate_id", maximum=MAX_CANDIDATE_ID_LENGTH)
    if not isinstance(payload["profile_json"], str):
        raise ValueError("profile_json должен быть строкой JSON UTF-8")
    return candidate_id, payload["profile_json"]


def _grid_bounds(rule: dict[str, int]) -> tuple[int, int]:
    start, end = rule["start_frequency_hz"], rule["end_frequency_hz"]
    origin, step = rule["origin_frequency_hz"], rule["step_hz"]
    # Modulo works for origins on either side of the band, including negative k.
    return start + (origin - start) % step, end - (end - origin) % step


def parse_frequency_rounding_profile(profile_json: str) -> dict[str, Any]:
    """Validate a raw UTF-8 artifact, retaining exact integer Hz values."""
    if not isinstance(profile_json, str):
        raise ValueError("profile_json должен быть строкой JSON UTF-8")
    try:
        size = len(profile_json.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError("Некорректный JSON UTF-8 профиля") from exc
    if size > MAX_PROFILE_BYTES:
        raise ValueError(f"Профиль должен занимать не более {MAX_PROFILE_BYTES} байт UTF-8")
    profile = strict_json_loads(profile_json)
    fields = {"format", "format_version", "name", "frequency_unit", "method", "tie_break", "rules"}
    if not isinstance(profile, dict) or set(profile) != fields:
        raise ValueError("Некорректные поля профиля округления частоты")
    if (profile["format"] != ROUNDING_FORMAT or type(profile["format_version"]) is not int
            or profile["format_version"] != 1):
        raise ValueError("Неподдерживаемый формат или версия профиля округления")
    if (profile["frequency_unit"] != "Hz" or profile["method"] != "nearest_grid"
            or profile["tie_break"] != "lower"):
        raise ValueError("Профиль требует Hz, nearest_grid и lower")
    name = normalize_reference_name(profile["name"])
    rules = profile["rules"]
    if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_ROUNDING_RULES:
        raise ValueError(f"Профиль должен содержать от 1 до {MAX_ROUNDING_RULES} правил")
    canonical_rules = []
    previous_end = -1
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != RULE_FIELDS:
            raise ValueError("Некорректные поля правила округления")
        if any(type(value) is not int or not 0 <= value <= MAX_FREQUENCY_HZ
               for value in rule.values()):
            raise ValueError("Поля правила должны быть безопасными неотрицательными целыми Hz")
        if rule["step_hz"] < 1:
            raise ValueError("step_hz должен быть положительным")
        start, end = rule["start_frequency_hz"], rule["end_frequency_hz"]
        if not previous_end < start <= end:
            raise ValueError("Диапазоны правил должны возрастать без пересечений включённых границ")
        first, last = _grid_bounds(rule)
        if first > last:
            raise ValueError("Диапазон правила должен содержать хотя бы одну точку сетки")
        canonical_rules.append(dict(rule))
        previous_end = end
    return {
        "format": ROUNDING_FORMAT, "format_version": 1, "name": name,
        "frequency_unit": "Hz", "method": "nearest_grid", "tie_break": "lower",
        "rules": canonical_rules,
    }


def _validate_source(session_id: str, candidate: Candidate) -> None:
    try:
        _text(session_id, "session_id")
        _text(candidate.session_id, "candidate.session_id")
        _text(candidate.id, "candidate.id", maximum=MAX_CANDIDATE_ID_LENGTH)
        _text(candidate.source_stream_id, "candidate.source_stream_id")
        _text(candidate.level_unit, "candidate.level_unit")
        if candidate.session_id != session_id:
            raise ValueError("Кандидат должен принадлежать выбранному сеансу")
        frequency = candidate.center_frequency_hz
        if type(frequency) is not int or not 1 <= frequency <= MAX_FREQUENCY_HZ:
            raise ValueError("Частота кандидата должна быть положительным безопасным целым Hz")
        interval = candidate.interval
        if not isinstance(interval, TimeInterval):
            raise ValueError("Кандидат должен содержать исходный интервал измерения")
        for value in (interval.started_at, interval.ended_at):
            if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
                raise ValueError("Интервал кандидата должен содержать время UTC")
        if interval.ended_at < interval.started_at:
            raise ValueError("Окончание интервала кандидата не может предшествовать началу")
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise FrequencyRoundingIncompatibleError(str(exc)) from exc


def calculate_candidate_frequency_rounding(
    session_id: str, candidate: Candidate, *, profile_json: str,
) -> dict[str, Any]:
    """Choose the nearest in-band grid point, ties lower, using only integers."""
    profile = parse_frequency_rounding_profile(profile_json)
    _validate_source(session_id, candidate)
    frequency = candidate.center_frequency_hz
    result = {
        "status": "outside_ranges", "rule_index": None, "nearest_frequency_hz": None,
        "offset_hz": None, "rounded_frequency_hz": None,
    }
    for index, rule in enumerate(profile["rules"]):
        if not rule["start_frequency_hz"] <= frequency <= rule["end_frequency_hz"]:
            continue
        first, last = _grid_bounds(rule)
        step, origin = rule["step_hz"], rule["origin_frequency_hz"]
        lower = origin + ((frequency - origin) // step) * step
        upper = lower + step
        # Clamp both neighbours to the rule's actual in-range grid, not its edges.
        lower, upper = max(first, min(last, lower)), max(first, min(last, upper))
        nearest = lower if abs(frequency - lower) <= abs(upper - frequency) else upper
        offset = nearest - frequency
        accepted = abs(offset) <= rule["max_offset_hz"]
        result = {
            "status": ("unchanged" if offset == 0 else "rounded") if accepted else "outside_tolerance",
            "rule_index": index, "nearest_frequency_hz": nearest, "offset_hz": offset,
            "rounded_frequency_hz": nearest if accepted else None,
        }
        break
    return {
        "session_id": session_id,
        "candidate": {
            "id": candidate.id, "source_stream_id": candidate.source_stream_id,
            "interval": to_primitive(candidate.interval),
            "center_frequency_hz": frequency, "level_unit": candidate.level_unit,
        },
        "profile": profile, "frequency_unit": "Hz", "method": "nearest_grid",
        "tie_break": "lower", "result": result,
    }
