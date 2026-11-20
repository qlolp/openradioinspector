"""Strict JSON acquisition settings and archived measurement metadata."""
from math import isfinite

from .sweep_comparison import SWEEP_METADATA_KEYS


def number(value):
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def json_value(value, nullable=False):
    if value is None:
        return nullable
    if type(value) in (str, bool):
        return True
    if type(value) in (int, float):
        return number(value)
    if isinstance(value, list):
        return all(json_value(item, nullable) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and json_value(item, nullable) for key, item in value.items())
    return False


def acquisition_settings(settings):
    if isinstance(settings, dict):
        recorded = {key: value for key, value in settings.items() if key not in SWEEP_METADATA_KEYS}
        if (recorded and json_value(recorded)
                and all(json_value(settings[key], nullable=True) for key in SWEEP_METADATA_KEYS if key in settings)):
            return recorded
    raise ValueError('Некорректные записанные настройки измерения')


def same_json(left, right):
    """JSON numeric equality never aliases booleans, including nested values."""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right
