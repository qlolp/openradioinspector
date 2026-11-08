"""Read-only presentation of the core's logical archive capacity."""

from __future__ import annotations

from typing import Any, Optional


CAPACITY_NOTE = (
    "Автоматическое удаление данных выключено. Лимит относится к записи измерений "
    "в логическую SQLite БД; WAL и изменения метаданных не ограничиваются им."
)
STATES = {"unlimited", "ok", "warning", "full", "unavailable"}


def validate_archive_capacity(value: Any) -> dict[str, Any]:
    """Reject malformed status instead of displaying a reassuring zero."""
    if not isinstance(value, dict):
        raise ValueError("Некорректный статус ёмкости архива")
    if (
        value.get("storage_mode") not in ("sqlite", "memory")
        or not isinstance(value.get("state"), str)
        or value["state"] not in STATES
    ):
        raise ValueError("Неизвестное состояние ёмкости архива")
    for key in ("database_bytes", "max_database_bytes", "remaining_bytes"):
        if key not in value:
            raise ValueError(f"В статусе ёмкости отсутствует {key}")
        size = value[key]
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise ValueError(f"Некорректное значение {key}")
    if not isinstance(value.get("measurement_writes_blocked"), bool):
        raise ValueError("Неизвестен статус записи измерений")
    if value.get("automatic_deletion") is not False or value.get("warning_threshold") != 0.9:
        raise ValueError("Неподдерживаемая политика ёмкости архива")
    if value["state"] in ("ok", "warning", "full") and (
        value["storage_mode"] != "sqlite"
        or value["database_bytes"] is None
        or not value["max_database_bytes"]
        or value["remaining_bytes"] is None
    ):
        raise ValueError("Неполные данные ёмкости SQLite")
    return value


def _size(value: Optional[int]) -> str:
    return "неизвестно" if value is None else f"{value:,} Б".replace(",", " ")


def archive_capacity_text(
    status: Optional[dict[str, Any]], error: Optional[str] = None
) -> str:
    if error:
        return f"Ёмкость архива: недоступна ({error})."
    if status is None:
        return "Ёмкость архива: состояние не получено."
    try:
        status = validate_archive_capacity(status)
    except ValueError as exc:
        return f"Ёмкость архива: недоступна ({exc})."
    if status["storage_mode"] == "memory":
        return "Архив в памяти: размер SQLite недоступен; дисковый лимит не применяется."
    used = _size(status["database_bytes"])
    limit = status["max_database_bytes"]
    prefix = f"Архив SQLite (логический): {used} / {_size(limit) if limit is not None else 'без лимита'}"
    state = status["state"]
    if state == "unavailable":
        return f"{prefix} · ёмкость недоступна; состояние записи неизвестно."
    if state == "unlimited":
        return f"{prefix} · лимит не задан."
    remaining = _size(status["remaining_bytes"])
    if state == "full":
        return f"{prefix} · ЛИМИТ ДОСТИГНУТ: запись новых измерений заблокирована."
    suffix = "ВНИМАНИЕ: занято ≥ 90% лимита" if state == "warning" else "в пределах лимита"
    if status["measurement_writes_blocked"]:
        suffix += " · запись новых измерений заблокирована"
    return f"{prefix} · осталось {remaining} · {suffix}."


def archive_capacity_color(status: Optional[dict[str, Any]], error: Optional[str] = None) -> str:
    if error or status is None:
        return "#666666"
    try:
        status = validate_archive_capacity(status)
    except ValueError:
        return "#666666"
    if status["state"] == "full" or status["measurement_writes_blocked"]:
        return "#b3261e"
    return "#8a5700" if status["state"] == "warning" else "#666666"
