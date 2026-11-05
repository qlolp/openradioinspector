"""Проверка форм Windows GUI и создание payload без измерительной логики."""

from __future__ import annotations

import math
from typing import Any, Optional


def required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} не должно быть пустым")
    return value.strip()


def required_int(value: Any, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} должно быть целым числом")
    try:
        parsed = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} должно быть целым числом") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label} должно быть целым числом")
    if parsed < minimum:
        raise ValueError(f"{label} должно быть не меньше {minimum}")
    return parsed


def optional_number(value: Any, label: str) -> Optional[float]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} должно быть числом") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} должно быть конечным числом")
    return parsed


def session_payload(
    *,
    name: Any,
    tinysa_port: Any,
    receiver_port: Any,
    start_hz: Any,
    stop_hz: Any,
    receiver_frequency_hz: Any,
    mode: Any,
    simulated: bool,
) -> dict[str, Any]:
    """Создаёт двухприборный профиль; симуляторы выбираются только явно."""
    selected_name = required_text(name, "Название сеанса")
    spectrum_port = required_text(tinysa_port, "Порт tinySA")
    target_port = required_text(receiver_port, "Порт AR-DV1")
    if spectrum_port == target_port:
        raise ValueError("Порты tinySA и AR-DV1 должны различаться")
    first_hz = required_int(start_hz, "Начальная частота")
    last_hz = required_int(stop_hz, "Конечная частота")
    if last_hz <= first_hz:
        raise ValueError("Конечная частота должна быть больше начальной")
    target_hz = required_int(receiver_frequency_hz, "Частота receiver")
    selected_mode = required_text(mode, "Режим receiver")
    if not isinstance(simulated, bool):
        raise ValueError("Признак симуляторов должен быть boolean")
    spectrum_settings: dict[str, Any] = {
        "start_hz": first_hz,
        "stop_hz": last_hz,
        "points": 201,
        "rbw_hz": 10000,
    }
    receiver_settings: dict[str, Any] = {
        "frequency_hz": target_hz,
        "mode": selected_mode,
        "squelch": 10,
    }
    if simulated:
        spectrum_settings.update(
            {"adapter": "tinysa-sim", "period_s": 0.25, "seed": 7}
        )
        receiver_settings.update(
            {"adapter": "ardv1-sim", "period_s": 0.25, "seed": 17}
        )
    return {
        "name": selected_name,
        "streams": [
            {
                "device": "tinySA Ultra",
                "role": "spectrum_analyzer",
                "level_unit": "dBm",
                "port": spectrum_port,
                "capabilities": ["sweep", "waterfall"],
                "settings": spectrum_settings,
            },
            {
                "device": "AR-DV1",
                "role": "receiver",
                "level_unit": "s_meter_count",
                "port": target_port,
                "capabilities": ["tune", "audio", "hardware_decode"],
                "settings": receiver_settings,
            },
        ],
        "metadata": {"created_by": "windows-gui", "simulated": simulated},
    }


def candidate_payload(
    stream: dict[str, Any],
    *,
    started_at: Any,
    ended_at: Any,
    center_frequency_hz: Any,
    bandwidth_hz: Any = "",
    peak_level: Any = "",
    score: Any = "",
    sweep_id: Optional[str] = None,
) -> dict[str, Any]:
    """Сохраняет исходную единицу потока и связь с реальным sweep, если она есть."""
    stream_id = required_text(stream.get("id"), "Исходный поток")
    level_unit = required_text(stream.get("level_unit"), "Единица потока")
    payload: dict[str, Any] = {
        "source_stream_id": stream_id,
        "interval": {
            "started_at": required_text(started_at, "Начало интервала"),
            "ended_at": required_text(ended_at, "Конец интервала"),
        },
        "center_frequency_hz": required_int(
            center_frequency_hz, "Центральная частота"
        ),
        "level_unit": level_unit,
        "observation_ids": [sweep_id] if sweep_id else [],
    }
    if bandwidth_hz is not None and str(bandwidth_hz).strip():
        payload["bandwidth_hz"] = required_int(bandwidth_hz, "Полоса")
    peak = optional_number(peak_level, "Исходный пик")
    if peak is not None:
        payload["peak_level"] = peak
    estimated_score = optional_number(score, "Оценка")
    if estimated_score is not None:
        payload["score"] = estimated_score
    return payload


def investigation_payload(
    *,
    target_stream_id: Any,
    target_frequency_hz: Any,
    mode: Any = "",
    priority: Any = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "target_stream_id": required_text(target_stream_id, "Receiver"),
        "target_frequency_hz": required_int(target_frequency_hz, "Частота handoff"),
        "priority": required_int(priority, "Приоритет", minimum=0),
    }
    if isinstance(mode, str) and mode.strip():
        payload["mode"] = mode.strip()
    return payload
