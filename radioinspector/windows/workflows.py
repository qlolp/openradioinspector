"""Проверка форм Windows GUI и создание payload без измерительной логики."""

from __future__ import annotations

import math
from typing import Any, Optional


def required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} не должно быть пустым")
    return value.strip()


def required_int(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum: Optional[int] = None,
) -> int:
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
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{label} должно быть не больше {maximum}")
    return parsed


def required_number(
    value: Any,
    label: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} должно быть конечным числом")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} должно быть числом") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{label} должно быть в диапазоне {minimum}…{maximum}")
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


def _enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Признак включения политики должен быть boolean")
    return value


def _optional_stream(value: Any, label: str) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return required_text(value, label)


def automatic_detection_payload(
    *,
    enabled: Any,
    interval_seconds: Any,
    stream_id: Any,
    threshold_dbm: Any,
    dedup_bandwidth_hz: Any,
    cooldown_seconds: Any,
    max_candidates_per_sweep: Any,
) -> dict[str, Any]:
    """Формирует независимую политику detector без операций handoff."""
    return {
        "enabled": _enabled(enabled),
        "interval_seconds": required_number(
            interval_seconds,
            "Период обнаружения",
            minimum=0.1,
            maximum=3600.0,
        ),
        "stream_id": _optional_stream(stream_id, "Поток обнаружения"),
        "threshold_dbm": required_number(
            threshold_dbm,
            "Порог dBm",
            minimum=-300.0,
            maximum=100.0,
        ),
        "dedup_bandwidth_hz": required_int(
            dedup_bandwidth_hz,
            "Полоса dedup",
            minimum=1,
            maximum=10_000_000_000,
        ),
        "cooldown_seconds": required_number(
            cooldown_seconds,
            "Cooldown",
            minimum=0.0,
            maximum=86400.0,
        ),
        "max_candidates_per_sweep": required_int(
            max_candidates_per_sweep,
            "Максимум кандидатов за sweep",
            minimum=1,
            maximum=100,
        ),
    }


def automatic_completion_payload(
    *,
    enabled: Any,
    interval_seconds: Any,
    minimum_listening_seconds: Any,
    minimum_observations: Any,
    receiver_stream_id: Any,
) -> dict[str, Any]:
    """Формирует completion payload; None сохраняет параметры без opt-in."""
    payload = {
        "interval_seconds": required_number(
            interval_seconds,
            "Период завершения",
            minimum=0.1,
            maximum=3600.0,
        ),
        "minimum_listening_seconds": required_number(
            minimum_listening_seconds,
            "Минимум прослушивания",
            minimum=0.0,
            maximum=86400.0,
        ),
        "minimum_observations": required_int(
            minimum_observations,
            "Минимум наблюдений",
            minimum=1,
            maximum=1000,
        ),
        "receiver_stream_id": _optional_stream(
            receiver_stream_id, "Receiver автоматического завершения"
        ),
    }
    if enabled is not None:
        return {"enabled": _enabled(enabled), **payload}
    return payload


def automatic_handoff_payload(
    *,
    enabled: Any,
    interval_seconds: Any,
    minimum_priority: Any,
    receiver_stream_id: Any,
    max_consecutive_failures: Any,
    retry_delay_seconds: Any,
) -> dict[str, Any]:
    """Формирует независимую политику scheduler без завершения исследований."""
    return {
        "enabled": _enabled(enabled),
        "interval_seconds": required_number(
            interval_seconds,
            "Период handoff",
            minimum=0.1,
            maximum=3600.0,
        ),
        "minimum_priority": required_int(
            minimum_priority,
            "Минимальный приоритет",
            minimum=0,
            maximum=1_000_000,
        ),
        "receiver_stream_id": _optional_stream(
            receiver_stream_id, "Receiver автоматического handoff"
        ),
        "max_consecutive_failures": required_int(
            max_consecutive_failures,
            "Сбоев до отключения",
            minimum=1,
            maximum=100,
        ),
        "retry_delay_seconds": required_number(
            retry_delay_seconds,
            "Задержка повтора",
            minimum=0.1,
            maximum=86400.0,
        ),
    }
