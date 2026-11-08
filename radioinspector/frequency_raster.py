"""Read-only rounding of detected signal frequencies by a portable raster table.

This is the fifth item of the functional backlog: «Округление частоты
обнаруженных сигналов по таблицам». The table is a portable artifact (like the
spectral mask) that maps frequency ranges to a channel raster. Source
frequencies of archived candidates are never changed: the rounded value is a
derived number returned beside the original, the same invariant as calibrated
levels being stored apart from raw ones.

Grid points of an entry are ``start_hz + k * grid_step_hz`` for
``k = 0 .. (stop_hz - start_hz) // grid_step_hz``. A frequency is rounded to
the nearest grid point of its entry; exact ties round to the higher grid
point. Frequencies outside every entry range are reported unmatched and left
without a rounded value instead of being extrapolated.
"""

from __future__ import annotations

import copy
from typing import Any

from .contracts import Candidate, to_primitive
from .spectral_mask import strict_json_loads
from .sweep_references import normalize_reference_name

RASTER_FORMAT = "openradioinspector-frequency-raster"
MAX_RASTER_BYTES = 65_536
MAX_ROUNDING_REQUEST_BYTES = 524_288
MAX_FREQUENCY_HZ = 9_007_199_254_740_991
MAX_RASTER_ENTRIES = 64
MAX_ROUNDING_CANDIDATES = 256
MAX_ID_LENGTH = 128


def validate_rounding_request(payload: Any) -> tuple[str, list[str]]:
    """Exact request shape: a table artifact and an ordered list of candidate IDs."""
    if not isinstance(payload, dict) or set(payload) != {"table_json", "candidate_ids"}:
        raise ValueError("Разрешены только поля table_json и candidate_ids")
    table_json = payload["table_json"]
    if not isinstance(table_json, str):
        raise ValueError("table_json должен быть строкой JSON UTF-8")
    identifiers = payload["candidate_ids"]
    if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= MAX_ROUNDING_CANDIDATES:
        raise ValueError(f"candidate_ids должен содержать от 1 до {MAX_ROUNDING_CANDIDATES} ID кандидатов")
    for value in identifiers:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_ID_LENGTH:
            raise ValueError("ID кандидата должен быть непустой строкой не длиннее 128 символов")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("ID кандидата должен быть корректной строкой UTF-8") from exc
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("candidate_ids не должны повторяться")
    return table_json, list(identifiers)


def parse_frequency_raster(table_json: str) -> dict[str, Any]:
    """Validate an imported raw document and return its canonical table."""
    if not isinstance(table_json, str):
        raise ValueError("table_json должен быть строкой JSON UTF-8")
    try:
        size = len(table_json.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError("Некорректный JSON UTF-8 таблицы") from exc
    if size > MAX_RASTER_BYTES:
        raise ValueError(f"Таблица должна занимать не более {MAX_RASTER_BYTES} байт UTF-8")
    table = strict_json_loads(table_json)
    fields = {"format", "format_version", "name", "frequency_unit", "entries"}
    if not isinstance(table, dict) or set(table) != fields:
        raise ValueError("Некорректные поля таблицы частотной сетки")
    if (table["format"] != RASTER_FORMAT or type(table["format_version"]) is not int
            or table["format_version"] != 1):
        raise ValueError("Неподдерживаемый формат или версия таблицы частотной сетки")
    if table["frequency_unit"] != "Hz":
        raise ValueError("Таблица требует единицу Hz")
    name = normalize_reference_name(table["name"])
    entries = table["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_RASTER_ENTRIES:
        raise ValueError(f"Таблица должна содержать от 1 до {MAX_RASTER_ENTRIES} записей")
    canonical = []
    previous_stop = -1
    seen_names = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "start_hz", "stop_hz", "grid_step_hz"}:
            raise ValueError("Запись требует только name, start_hz, stop_hz и grid_step_hz")
        entry_name = normalize_reference_name(entry["name"])
        if entry_name in seen_names:
            raise ValueError("Имена записей таблицы не должны повторяться")
        seen_names.add(entry_name)
        start, stop, step = entry["start_hz"], entry["stop_hz"], entry["grid_step_hz"]
        for value, label in ((start, "start_hz"), (stop, "stop_hz"), (step, "grid_step_hz")):
            if type(value) is not int:
                raise ValueError(f"{label} должен быть целым числом")
        if not 0 <= start < stop <= MAX_FREQUENCY_HZ:
            raise ValueError("Диапазон записи должен удовлетворять 0 <= start_hz < stop_hz в безопасных целых Hz")
        if step < 1:
            raise ValueError("grid_step_hz должен быть положительным")
        if step > stop - start:
            raise ValueError("grid_step_hz не должен превышать ширину диапазона записи")
        if start <= previous_stop:
            raise ValueError("Диапазоны записей не должны пересекаться и должны идти по возрастанию")
        previous_stop = stop
        canonical.append({
            "name": entry_name, "start_hz": start, "stop_hz": stop, "grid_step_hz": step,
        })
    return {
        "format": RASTER_FORMAT, "format_version": 1, "name": name,
        "frequency_unit": "Hz", "entries": canonical,
    }


def round_frequency(frequency_hz: int, table: dict[str, Any]) -> dict[str, Any]:
    """Round one integer frequency to the nearest grid point of its entry."""
    if type(frequency_hz) is not int or not 0 <= frequency_hz <= MAX_FREQUENCY_HZ:
        raise ValueError("Частота должна быть целым числом в диапазоне безопасных Hz")
    entry = next(
        (item for item in table["entries"]
         if item["start_hz"] <= frequency_hz <= item["stop_hz"]),
        None,
    )
    if entry is None:
        return {
            "frequency_hz": frequency_hz, "matched": False,
            "rounded_frequency_hz": None, "delta_hz": None,
            "grid_step_hz": None, "entry_name": None,
        }
    start, stop, step = entry["start_hz"], entry["stop_hz"], entry["grid_step_hz"]
    distance = frequency_hz - start
    # Exact integer half-up: (2*d + step) // (2*step) rounds d/step with
    # halves toward the higher grid point, without float division.
    index = (2 * distance + step) // (2 * step)
    index = min(max(index, 0), (stop - start) // step)
    rounded = start + index * step
    return {
        "frequency_hz": frequency_hz, "matched": True,
        "rounded_frequency_hz": rounded, "delta_hz": rounded - frequency_hz,
        "grid_step_hz": step, "entry_name": entry["name"],
    }


def calculate_frequency_rounding(
    session_id: str, candidates: list[Candidate], table: dict[str, Any],
) -> dict[str, Any]:
    """Round archived candidate frequencies without changing the candidates.

    Request order is preserved; unmatched frequencies are counted, never
    rounded to a guessed entry. The source ``center_frequency_hz`` is echoed
    from the archive exactly as recorded. ``table`` is the canonical artifact
    from :func:`parse_frequency_raster`, which runs before any candidate
    lookup so a malformed table never depends on archive contents.
    """
    items = []
    for candidate in candidates:
        result = round_frequency(candidate.center_frequency_hz, table)
        items.append({"candidate_id": candidate.id, **result})
    matched = sum(1 for item in items if item["matched"])
    return {
        "session_id": session_id, "order": "request",
        "table": copy.deepcopy(table), "count": len(items),
        "matched_count": matched, "unmatched_count": len(items) - matched,
        "items": items,
    }
