"""Immutable labels for archived comparison sweeps, without measurement copies."""

from __future__ import annotations

import unicodedata
from typing import Any

MAX_SWEEP_REFERENCES = 100
MAX_REFERENCE_NAME_LENGTH = 80


class ReferenceNameConflictError(ValueError):
    """A session already uses this exact normalized name for another sweep."""


class ReferenceLimitReachedError(ValueError):
    """The session catalog is full; existing references remain usable."""


def normalize_reference_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Имя эталона должно быть строкой")
    # Reject controls before trimming too: a trailing newline must not silently
    # become an accepted name. C* includes format controls and lone surrogates.
    normalized = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise ValueError("Имя эталона не должно содержать управляющие символы Unicode")
    normalized = normalized.strip()
    if not 1 <= len(normalized) <= MAX_REFERENCE_NAME_LENGTH:
        raise ValueError(f"Имя эталона должно содержать 1–{MAX_REFERENCE_NAME_LENGTH} символов")
    return normalized


def check_reference_creation(
    items: list[dict[str, Any]], name: str, sweep_id: str,
) -> dict[str, Any] | None:
    """Shared memory/SQLite name and capacity policy, evaluated under a lock."""
    existing = next((item for item in items if item["name"] == name), None)
    if existing is not None:
        if existing["sweep_id"] != sweep_id:
            raise ReferenceNameConflictError("Это имя эталона уже используется для другого свипа")
        return existing
    if len(items) >= MAX_SWEEP_REFERENCES:
        raise ReferenceLimitReachedError(f"В сеансе уже сохранено {MAX_SWEEP_REFERENCES} эталонов")
    return None
