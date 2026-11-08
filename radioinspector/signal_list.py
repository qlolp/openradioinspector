"""Portable signal-list exchange between instruments and instances.

Backlog item 12: «Загрузка списка сигналов по сети» — the original's
ISBDViewer does «Load from network (from other instrument)» and «Save
RadioInspector signal's data to file». This module defines the portable
artifact and the strict shape of one entry: an observed frequency with its
bandwidth, level and the interval it was seen in.

Export serializes archived candidates of a session; import creates new
candidates on a chosen stream through the same lifecycle as manual
creation. The artifact carries observations, never states: imported
signals always start as ``new``. Levels keep their unit — no automatic
conversion between ``dBm`` and ``s_meter_count`` ever happens here.
"""

from __future__ import annotations

from typing import Any

import json

from .contracts import Candidate, TimeInterval, to_primitive
from .spectral_mask import strict_json_loads
from .sweep_references import normalize_reference_name

SIGNAL_LIST_FORMAT = "openradioinspector-signal-list"
MAX_SIGNAL_LIST_BYTES = 262_144
MAX_SIGNAL_LIST_ENTRIES = 256
MAX_FREQUENCY_HZ = 9_007_199_254_740_991
MAX_IMPORT_REQUEST_BYTES = 524_288


class SignalListError(ValueError):
    """The artifact or the target stream cannot support this exchange."""


def parse_signal_list(list_json: str) -> dict[str, Any]:
    """Validate an imported raw artifact and return its canonical form."""
    if not isinstance(list_json, str):
        raise ValueError("list_json должен быть строкой JSON UTF-8")
    try:
        size = len(list_json.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError("Некорректный JSON UTF-8 списка сигналов") from exc
    if size > MAX_SIGNAL_LIST_BYTES:
        raise ValueError(f"Список сигналов должен занимать не более {MAX_SIGNAL_LIST_BYTES} байт UTF-8")
    document = strict_json_loads(list_json)
    fields = {"format", "format_version", "name", "frequency_unit", "entries"}
    if not isinstance(document, dict) or set(document) != fields:
        raise ValueError("Некорректные поля списка сигналов")
    if (document["format"] != SIGNAL_LIST_FORMAT or type(document["format_version"]) is not int
            or document["format_version"] != 1):
        raise ValueError("Неподдерживаемый формат или версия списка сигналов")
    if document["frequency_unit"] != "Hz":
        raise ValueError("Список сигналов требует единицу Hz")
    name = normalize_reference_name(document["name"])
    entries = document["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SIGNAL_LIST_ENTRIES:
        raise ValueError(f"Список сигналов должен содержать от 1 до {MAX_SIGNAL_LIST_ENTRIES} записей")
    canonical = []
    for entry in entries:
        canonical.append(_validate_entry(entry))
    return {
        "format": SIGNAL_LIST_FORMAT, "format_version": 1, "name": name,
        "frequency_unit": "Hz", "entries": canonical,
    }


def _validate_entry(entry: Any) -> dict[str, Any]:
    fields = {"frequency_hz", "bandwidth_hz", "interval", "level_unit", "peak_level"}
    if not isinstance(entry, dict) or set(entry) != fields:
        raise ValueError("Запись требует только frequency_hz, bandwidth_hz, interval, level_unit и peak_level")
    frequency = entry["frequency_hz"]
    if isinstance(frequency, bool) or not isinstance(frequency, int) or not 0 <= frequency <= MAX_FREQUENCY_HZ:
        raise ValueError("frequency_hz записи должен быть целым числом в диапазоне безопасных Hz")
    bandwidth = entry["bandwidth_hz"]
    if bandwidth is not None and (
            isinstance(bandwidth, bool) or not isinstance(bandwidth, int)
            or not 0 <= bandwidth <= MAX_FREQUENCY_HZ):
        raise ValueError("bandwidth_hz записи должен быть целым числом Hz или null")
    interval = entry["interval"]
    if not isinstance(interval, dict) or set(interval) != {"started_at", "ended_at"}:
        raise ValueError("interval записи должен содержать только started_at и ended_at")
    for key in ("started_at", "ended_at"):
        if not isinstance(interval[key], str) or not interval[key]:
            raise ValueError(f"{key} записи должен быть непустой строкой UTC")
    interval = to_primitive(TimeInterval.from_dict(interval))
    level_unit = entry["level_unit"]
    if not isinstance(level_unit, str) or level_unit not in ("dBm", "s_meter_count"):
        raise ValueError("level_unit записи должен быть dBm или s_meter_count")
    peak = entry["peak_level"]
    if peak is not None and (isinstance(peak, bool) or not isinstance(peak, (int, float))):
        raise ValueError("peak_level записи должен быть числом или null")
    if isinstance(peak, float) and (peak != peak or peak in (float("inf"), float("-inf"))):
        raise ValueError("peak_level записи должен быть конечным числом")
    return {
        "frequency_hz": frequency,
        "bandwidth_hz": bandwidth,
        "interval": dict(interval),
        "level_unit": level_unit,
        "peak_level": peak,
    }


def build_signal_list(candidates: list[Candidate], name: str) -> dict[str, Any]:
    """Export archived candidates as a portable artifact; sources stay unchanged.

    The artifact carries exactly the importable fields so a saved file can be
    loaded back unchanged — session metadata stays in the response envelope.
    """
    entries = []
    for candidate in candidates:
        if candidate.level_unit not in ("dBm", "s_meter_count"):
            raise SignalListError(f"Неизвестная единица кандидата: {candidate.level_unit}")
        entries.append({
            "frequency_hz": candidate.center_frequency_hz,
            "bandwidth_hz": candidate.bandwidth_hz,
            "interval": to_primitive(candidate.interval),
            "level_unit": candidate.level_unit,
            "peak_level": candidate.peak_level,
        })
    if not entries:
        raise SignalListError("В сеансе нет кандидатов для экспорта")
    artifact = {
        "format": SIGNAL_LIST_FORMAT, "format_version": 1,
        "name": normalize_reference_name(name),
        "frequency_unit": "Hz", "entries": entries,
    }
    # An export must be accepted unchanged by the importer, including limits.
    return parse_signal_list(json.dumps(artifact, ensure_ascii=False, allow_nan=False))


def import_candidates(
    stream_id: str, level_unit: str, artifact: dict[str, Any],
) -> list[dict[str, Any]]:
    """Turn artifact entries into ``new`` candidate payloads for one stream.

    The stream's level unit must match each entry exactly — levels are never
    converted. Interval, frequency and bandwidth are copied verbatim; the
    result carries no state, because imported signals start as ``new``.
    """
    if not isinstance(stream_id, str) or not stream_id.strip():
        raise SignalListError("stream_id должен быть непустым ID потока")
    payloads = []
    for entry in artifact["entries"]:
        if entry["level_unit"] != level_unit:
            raise SignalListError(
                f"Единица записи {entry['level_unit']} не совпадает с потоком {level_unit}; автопреобразование запрещено"
            )
        payloads.append({
            "source_stream_id": stream_id,
            "interval": dict(entry["interval"]),
            "center_frequency_hz": entry["frequency_hz"],
            "bandwidth_hz": entry["bandwidth_hz"],
            "peak_level": entry["peak_level"],
            "level_unit": level_unit,
        })
    return payloads
