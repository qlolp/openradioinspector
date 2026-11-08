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
    return 10.0 ** (value / 20.0)


def v_to_dbmv(value: float) -> float:
    return 20.0 * math.log10(value)


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
    """Переводит физическую величину из одной единицы в другую.

    Параметры
    ----------
    value:
        Исходное значение.
    from_unit, to_unit:
        Обозначения единиц (регистр и кириллица в обозначениях допускаются).
    impedance:
        Сопротивление нормирующего тракта, Ом. Используется только для
        переходов напряжение <-> мощность.
    antenna_factor_db:
        Коэффициент усиления антенны, дБ. Используется при переходе между
        напряжённостью поля и мощностью на входе приёмника.

    Замечание
    ---------
    Переход «напряжённость поля → мощность на входе приёмника» выполняется как
    ``P_dBm = E_dBuV/m + A_dB - 107`` для 50-омного тракта, что эквивалентно
    учёту коэффициента калибровки измерительного тракта на частоте измерений.
    """
    src = normalize_unit(from_unit)
    dst = normalize_unit(to_unit)
    if src == dst:
        return value
    if impedance <= 0:
        raise UnitError("Сопротивление тракта должно быть положительным")

    src_kind = _kind(src)
    dst_kind = _kind(dst)

    # 1. Переводы внутри одной группы выполняются напрямую — так не теряется
    #    точность и не требуется знать сопротивление тракта.
    if src_kind == dst_kind:
        if src_kind == "power":
            if dst in POWER_ABS:
                return _watts_to_power_unit(_power_to_watts(value, src), dst)
            return _watts_to_target(_watts_to_dbm(value, src), dst, impedance)
        if src_kind == "voltage":
            if dst in VOLTAGE_ABS:
                return _volts_to_voltage_unit(_voltage_to_volts(value, src), dst)
            return _watts_to_target(_watts_to_dbm(value, src), dst, impedance)
        if dst in FIELD_ABS:
            return _v_per_m_to_field_unit(_field_to_v_per_m(value, src), dst)
        return _watts_to_target(_watts_to_dbm(value, src), dst, impedance)

    # 2. Приводим исходную величину к канонической форме своей группы.
    if src_kind == "power":
        watts = _power_to_watts(value, src)
    elif src_kind == "voltage":
        volts = _voltage_to_volts(value, src)
        watts = volts * volts / impedance
    else:  # field
        v_per_m = _field_to_v_per_m(value, src)
        watts = v_per_m * v_per_m / impedance

    # 3. Учитываем коэффициент усиления антенны при переходе из поля.
    if src_kind == "field" and dst_kind == "power":
        dbm = mw_to_dbm(watts * 1000.0) + antenna_factor_db
        return _watts_to_target(dbm, dst, impedance)

    # 4. В поле мощности через калибровочный коэффициент.
    if dst_kind == "field":
        dbm = mw_to_dbm(watts * 1000.0) + antenna_factor_db
        # Обратный переход: E_dBuV/m = P_dBm - A_dB + 107
        return _watts_to_target(dbm - antenna_factor_db, dst, impedance)

    dbm = mw_to_dbm(watts * 1000.0)
    return _watts_to_target(dbm, dst, impedance)


def _watts_to_dbm(value: float, unit: str) -> float:
    """Приведение величины мощности к дБм."""
    if unit == "dBm":
        return value
    if unit == "dBW":
        return value + 30.0
    return mw_to_dbm(_power_to_watts(value, unit) * 1000.0)


def _watts_to_power_unit(watts: float, unit: str) -> float:
    """Представление мощности в абсолютной единице группы."""
    if unit == "W":
        return watts
    if unit == "mW":
        return watts * 1e3
    if unit == "uW":
        return watts * 1e6
    raise UnitError(f"Неизвестная единица мощности: {unit}")


def _volts_to_voltage_unit(volts: float, unit: str) -> float:
    """Представление напряжения в абсолютной единице группы."""
    if unit == "V":
        return volts
    if unit == "mV":
        return volts * 1e3
    if unit == "uV":
        return volts * 1e6
    raise UnitError(f"Неизвестная единица напряжения: {unit}")


def _v_per_m_to_field_unit(v_per_m: float, unit: str) -> float:
    """Представление напряжённости поля в абсолютной единице группы."""
    if unit == "V/m":
        return v_per_m
    if unit == "mV/m":
        return v_per_m * 1e3
    if unit == "uV/m":
        return v_per_m * 1e6
    raise UnitError(f"Неизвестная единица напряжённости поля: {unit}")


def _watts_to_target(dbm: float, dst: str, impedance: float = DEFAULT_IMPEDANCE) -> float:
    kind = _kind(dst)
    if kind == "power":
        watts = dbm_to_w(dbm)
        return {
            "dBW": w_to_dbw(watts),
            "dBm": dbm,
            "W": watts,
            "mW": watts * 1e3,
            "uW": watts * 1e6,
        }[dst]
    if kind == "voltage":
        volts = math.sqrt(dbm_to_w(dbm) * impedance)
        return {
            "dBmV": v_to_dbmv(volts),
            "dBuV": uv_to_dbuv(volts * 1e6),
            "V": volts,
            "mV": volts * 1e3,
            "uV": volts * 1e6,
        }[dst]
    v_per_m = math.sqrt(dbm_to_w(dbm) * impedance)
    return {
        "dBuV/m": v_per_m_to_dbuv_per_m(v_per_m),
        "dBV/m": v_per_m_to_dbv_per_m(v_per_m),
        "V/m": v_per_m,
        "mV/m": v_per_m * 1e3,
        "uV/m": v_per_m * 1e6,
    }[dst]


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
