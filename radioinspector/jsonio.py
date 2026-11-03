"""Подготовка структур данных к сериализации в JSON.

Стандартный модуль ``json`` по умолчанию выводит нечисловые значения ``NaN``
и ``Infinity``, которые не являются допустимым JSON и не принимаются
большинством клиентов (в частности, ``JSON.parse`` в браузере). Модуль
заменяет такие значения на ``null`` и обеспечивает корректную сериализацию
объектов datetime, numpy и перечислений.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np


def to_jsonable(value: Any) -> Any:
    """Рекурсивное приведение значения к JSON-совместимому виду.

    Нечисловые значения ``float`` (``NaN``, ``Infinity``) заменяются на
    ``None``; объекты datetime, numpy, Path, Decimal и Enum приводятся к
    понятным текстовым или числовым представлениям.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.ndarray):
        return to_jsonable(value.tolist())
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return str(value)


def dumps(value: Any, indent: int | None = None) -> str:
    """Сериализация значения в строку JSON без недопустимых литералов."""
    return json.dumps(
        to_jsonable(value), ensure_ascii=False, indent=indent, allow_nan=False
    )
