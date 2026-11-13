"""Единицы измерения и преобразования между ними.

Реализует набор преобразований, описанных в разделе 2 «Работа с калькулятором и
записной книжкой» руководства оператора Книги 6 исходного комплекса:

    дБмкВ, дБм (мВт), дБмВ, дБВт, мкВ, В, мкВт, мВт, Вт

Сверх того добавлены преобразования напряжённости электрического поля
(дБмкВ/м, В/м, мкВ/м) — они нужны для измерения напряжённости поля и построения
карт покрытия.

Все функции работают с вещественными числами и не зависят от numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

#: Сопротивление нормирующего тракта, Ом. Используется для переходов
#: между напряжением и мощностью.
DEFAULT_IMPEDANCE = 50.0

#: Поддерживаемые единицы.
POWER_DB = ("dBW", "dBm")
POWER_ABS = ("W", "mW", "uW")
VOLTAGE_DB = ("dBmV", "dBuV")
VOLTAGE_ABS = ("V", "mV", "uV")
FIELD_DB = ("dBuV/m", "dBV/m")
FIELD_ABS = ("V/m", "mV/m", "uV/m")

ALL_UNITS = (
    POWER_DB
    + POWER_ABS
    + VOLTAGE_DB
    + VOLTAGE_ABS
    + FIELD_DB
    + FIELD_ABS
)


class UnitError(ValueError):
    """Неизвестная или несовместимая единица измерения."""


def normalize_unit(unit: str) -> str:
    """Приводит обозначение единицы к каноническому виду.

    ``дБмкВ``, ``dBuV``, ``дБмкв`` → ``dBuV``.
    """
    raw = (unit or "").strip()
    if not raw:
        raise UnitError("Пустое обозначение единицы измерения")
    table = {
        "дбмкв": "dBuV",
        "дбмкв/м": "dBuV/m",
        "дбмв": "dBmV",
        "дбм": "dBm",
        "дбвт": "dBW",
        "дбв/м": "dBV/m",
        "dbuv": "dBuV",
        "dbuv/m": "dBuV/m",
        "dbmv": "dBmV",
        "dbm": "dBm",
        "dbw": "dBW",
        "dbv/m": "dBV/m",
    }
    key = raw.lower().replace(" ", "")
    if key in table:
        return table[key]
    # µV/uV variants written with the Greek mu.
    key = key.replace("µ", "u").replace("μ", "u")
    return {"uv": "uV", "mv": "mV", "uw": "uW"}.get(key, raw)


def is_power_unit(unit: str) -> bool:
    unit = normalize_unit(unit)
    return unit in POWER_DB or unit in POWER_ABS


def is_voltage_unit(unit: str) -> bool:
    unit = normalize_unit(unit)
    return unit in VOLTAGE_DB or unit in VOLTAGE_ABS


def is_field_unit(unit: str) -> bool:
    unit = normalize_unit(unit)
    return unit in FIELD_DB or unit in FIELD_ABS


# --------------------------------------------------------------------------
# Мощность
# --------------------------------------------------------------------------

def dbm_to_mw(value: float) -> float:
    return 10.0 ** (value / 10.0)


def mw_to_dbm(value: float) -> float:
    return 10.0 * math.log10(value)


def dbw_to_w(value: float) -> float:
    return 10.0 ** (value / 10.0)


def w_to_dbw(value: float) -> float:
    return 10.0 * math.log10(value)


def dbm_to_w(value: float) -> float:
    return dbm_to_mw(value) / 1000.0


def w_to_dbm(value: float) -> float:
    return mw_to_dbm(value * 1000.0)


# --------------------------------------------------------------------------
# Напряжение
# --------------------------------------------------------------------------

def dbuv_to_uv(value: float) -> float:
    return 10.0 ** (value / 20.0)


def uv_to_dbuv(value: float) -> float:
    return 20.0 * math.log10(value)


def dbmv_to_v(value: float) -> float:
    return 1e-3 * 10.0 ** (value / 20.0)


def v_to_dbmv(value: float) -> float:
    return 20.0 * math.log10(value / 1e-3)


# --------------------------------------------------------------------------
# Напряжённость электрического поля
# --------------------------------------------------------------------------

def dbuv_per_m_to_v_per_m(value: float) -> float:
    return 10.0 ** (value / 20.0) * 1e-6


def v_per_m_to_dbuv_per_m(value: float) -> float:
    return 20.0 * math.log10(value / 1e-6)


def dbv_per_m_to_v_per_m(value: float) -> float:
    return 10.0 ** (value / 20.0)


def v_per_m_to_dbv_per_m(value: float) -> float:
    return 20.0 * math.log10(value)


# --------------------------------------------------------------------------
# Общий конвертер
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Conversion:
    """Результат преобразования величины."""

    value: float
    unit: str


def convert(
    value: float,
    from_unit: str,
    to_unit: str,
    impedance: float = DEFAULT_IMPEDANCE,
    antenna_factor_db: float = 0.0,
) -> float:
    """Convert RMS terminal voltage, power and electric field units.

    ``impedance`` is the real reference impedance in ohms. The antenna
    factor is E/U, expressed in dB/m: E[dBuV/m] = U[dBuV] + AF[dB/m].
    Its default zero is a numerical assumption, not an antenna calibration.
    Use field_strength_rms_v2 for a traceable physical field estimate.
    """
    src = normalize_unit(from_unit)
    dst = normalize_unit(to_unit)
    src_kind, dst_kind = _kind(src), _kind(dst)
    if not all(math.isfinite(v) for v in (value, impedance, antenna_factor_db)):
        raise UnitError("Значение и параметры преобразования должны быть конечными")
    if impedance <= 0:
        raise UnitError("Сопротивление тракта должно быть положительным")
    if src in POWER_ABS + VOLTAGE_ABS + FIELD_ABS and value < 0:
        raise UnitError("Мощность, RMS-напряжение и модуль поля не могут быть отрицательными")
    if src == dst:
        return value
    try:
        if src_kind == dst_kind:
            if src_kind == "power":
                result = _watts_to_power_unit(_power_to_watts(value, src), dst)
            elif src_kind == "voltage":
                result = _volts_to_voltage_unit(_voltage_to_volts(value, src), dst)
            else:
                result = _v_per_m_to_field_unit(_field_to_v_per_m(value, src), dst)
            if not math.isfinite(result):
                raise UnitError("Результат преобразования вне конечного диапазона")
            return result

        # Normalize at the receiver input reference plane, using RMS volts.
        if src_kind == "power":
            volts = math.sqrt(_power_to_watts(value, src) * impedance)
        elif src_kind == "voltage":
            volts = _voltage_to_volts(value, src)
        else:
            volts = _field_to_v_per_m(value, src) / 10.0 ** (antenna_factor_db / 20.0)
        if dst_kind == "power":
            result = _watts_to_power_unit(volts * volts / impedance, dst)
        elif dst_kind == "voltage":
            result = _volts_to_voltage_unit(volts, dst)
        else:
            result = _v_per_m_to_field_unit(volts * 10.0 ** (antenna_factor_db / 20.0), dst)
        if not math.isfinite(result):
            raise UnitError("Результат преобразования вне конечного диапазона")
        return result
    except (OverflowError, ZeroDivisionError, ValueError) as exc:
        raise UnitError("Значение вне области преобразования единиц") from exc


def _watts_to_power_unit(watts: float, unit: str) -> float:
    """Представление мощности в абсолютной единице группы."""
    if unit == "dBW":
        return w_to_dbw(watts)
    if unit == "dBm":
        return w_to_dbm(watts)
    if unit == "W":
        return watts
    if unit == "mW":
        return watts * 1e3
    if unit == "uW":
        return watts * 1e6
    raise UnitError(f"Неизвестная единица мощности: {unit}")


def _volts_to_voltage_unit(volts: float, unit: str) -> float:
    """Представление напряжения в абсолютной единице группы."""
    if unit == "dBmV":
        return v_to_dbmv(volts)
    if unit == "dBuV":
        return uv_to_dbuv(volts * 1e6)
    if unit == "V":
        return volts
    if unit == "mV":
        return volts * 1e3
    if unit == "uV":
        return volts * 1e6
    raise UnitError(f"Неизвестная единица напряжения: {unit}")


def _v_per_m_to_field_unit(v_per_m: float, unit: str) -> float:
    """Представление напряжённости поля в абсолютной единице группы."""
    if unit == "dBuV/m":
        return v_per_m_to_dbuv_per_m(v_per_m)
    if unit == "dBV/m":
        return v_per_m_to_dbv_per_m(v_per_m)
    if unit == "V/m":
        return v_per_m
    if unit == "mV/m":
        return v_per_m * 1e3
    if unit == "uV/m":
        return v_per_m * 1e6
    raise UnitError(f"Неизвестная единица напряжённости поля: {unit}")


def _kind(unit: str) -> str:
    if is_power_unit(unit):
        return "power"
    if is_voltage_unit(unit):
        return "voltage"
    if is_field_unit(unit):
        return "field"
    raise UnitError(f"Неизвестная единица измерения: {unit}")


def _power_to_watts(value: float, unit: str) -> float:
    if unit == "dBW":
        return dbw_to_w(value)
    if unit == "dBm":
        return dbm_to_w(value)
    if unit == "W":
        return value
    if unit == "mW":
        return value / 1e3
    if unit == "uW":
        return value / 1e6
    raise UnitError(f"Неизвестная единица мощности: {unit}")


def _voltage_to_volts(value: float, unit: str) -> float:
    if unit == "dBmV":
        return dbmv_to_v(value)
    if unit == "dBuV":
        return dbuv_to_uv(value) * 1e-6
    if unit == "V":
        return value
    if unit == "mV":
        return value / 1e3
    if unit == "uV":
        return value / 1e6
    raise UnitError(f"Неизвестная единица напряжения: {unit}")


def _field_to_v_per_m(value: float, unit: str) -> float:
    if unit == "dBuV/m":
        return dbuv_per_m_to_v_per_m(value)
    if unit == "dBV/m":
        return dbv_per_m_to_v_per_m(value)
    if unit == "V/m":
        return value
    if unit == "mV/m":
        return value / 1e3
    if unit == "uV/m":
        return value / 1e6
    raise UnitError(f"Неизвестная единица напряжённости поля: {unit}")


def convert_series(
    values: Iterable[float],
    from_unit: str,
    to_unit: str,
    impedance: float = DEFAULT_IMPEDANCE,
    antenna_factor_db: float = 0.0,
) -> list[float]:
    """Поточечное преобразование последовательности значений."""
    return [
        convert(v, from_unit, to_unit, impedance, antenna_factor_db) for v in values
    ]


def format_unit_catalog() -> list[tuple[str, str]]:
    """Возвращает перечень поддерживаемых единиц с описанием группы."""
    catalog: list[tuple[str, str]] = []
    for unit in ALL_UNITS:
        catalog.append((unit, _kind(unit)))
    return catalog
