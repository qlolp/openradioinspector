"""Narrowband CW RMS-equivalent field, with an explicit calibration snapshot.

No calibration is inferred from a device name, frequency band, dBFS or S-meter.
An operator-supplied profile is evidence of the assumptions, not certification.
"""
from __future__ import annotations

from bisect import bisect_left
from copy import deepcopy
import hashlib
import json
import math

from .contracts import Sweep, TimeInterval, to_primitive
from .units import convert, is_field_unit

MAX_CALIBRATION_POINTS = 128
MAX_FIELD_REQUEST_BYTES = 65536
MAX_HZ = 9_007_199_254_740_991


class FieldStrengthUnavailable(ValueError):
    """The source and calibration do not establish applicability of this method."""


def _number(value, name, *, minimum=None):
    if (type(value) not in (float, int) or not math.isfinite(value)
            or (minimum is not None and value < minimum)):
        raise ValueError(f"{name}: требуется конечное число в допустимом диапазоне")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{name}: требуется непустая строка до 512 символов")
    value.encode('utf-8', errors='strict')
    return value


def _frequency(value):
    if type(value) is not int or not 0 <= value <= MAX_HZ:
        raise ValueError('frequency_hz: требуются целые неотрицательные Гц')
    return value


def validate_calibration(profile):
    fields = {'id', 'version', 'instrument_profile', 'measurement_chain_id', 'reference_plane', 'impedance_ohm',
              'detector', 'rbw_hz', 'waveform', 'valid_from', 'valid_until',
              'uncertainty_basis', 'points'}
    if not isinstance(profile, dict) or set(profile) != fields:
        raise ValueError('Неполный или неизвестный формат калибровочного профиля')
    for name in ('id', 'instrument_profile', 'measurement_chain_id', 'reference_plane', 'uncertainty_basis'):
        _text(profile[name], name)
    if type(profile['version']) is not int or not 1 <= profile['version'] <= MAX_HZ:
        raise ValueError('version: требуется положительное целое число')
    if profile['detector'] != 'RMS' or profile['waveform'] != 'CW':
        raise ValueError('Метод v2 поддерживает только подтверждённые RMS/CW условия')
    _number(profile['impedance_ohm'], 'impedance_ohm', minimum=1e-12)
    _number(profile['rbw_hz'], 'rbw_hz', minimum=1e-12)
    TimeInterval.from_dict({'started_at': profile['valid_from'], 'ended_at': profile['valid_until']})
    points = profile['points']
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_CALIBRATION_POINTS:
        raise ValueError('Нужно от 1 до 128 калибровочных точек')
    point_fields = {'frequency_hz', 'antenna_factor_db_per_m', 'cable_loss_db',
                    'preamp_gain_db', 'level_correction_db', 'uncertainty_db'}
    previous = -1
    for point in points:
        if not isinstance(point, dict) or set(point) != point_fields:
            raise ValueError('Неполная калибровочная точка')
        frequency = _frequency(point['frequency_hz'])
        if frequency <= previous:
            raise ValueError('Калибровочные частоты должны строго возрастать')
        previous = frequency
        for name in point_fields - {'frequency_hz', 'uncertainty_db'}:
            _number(point[name], name)
        if point['uncertainty_db'] is not None:
            _number(point['uncertainty_db'], 'uncertainty_db', minimum=0)
    return deepcopy(profile)


def validate_field_request(payload):
    if not isinstance(payload, dict) or set(payload) != {'sweep_id', 'frequency_hz', 'calibration'}:
        raise ValueError('Требуются только sweep_id, frequency_hz и calibration')
    _text(payload['sweep_id'], 'sweep_id')
    _frequency(payload['frequency_hz'])
    return {**payload, 'calibration': validate_calibration(payload['calibration'])}


def field_strength_rms_v2(sweep: Sweep, calibration: dict, *, frequency_hz: int) -> dict:
    """Estimate one recorded bin, without signal interpolation or extrapolation.

    U_rms=sqrt(P*R); E[dBuV/m]=U[dBuV]+AF+cable-preamp+level correction.
    Physical field inputs already include tract corrections: apply none twice.
    Profile uncertainty is a declared combined dB uncertainty, not derived from
    the scatter of bins. None remains unknown, never silently zero.
    """
    profile = validate_calibration(calibration)
    _frequency(frequency_hz)
    validity = TimeInterval.from_dict({'started_at': profile['valid_from'], 'ended_at': profile['valid_until']})
    if not (validity.started_at <= sweep.interval.started_at <= sweep.interval.ended_at <= validity.ended_at):
        raise FieldStrengthUnavailable('Калибровка не покрывает интервал исходного измерения')
    for name in ('instrument_profile', 'measurement_chain_id', 'reference_plane', 'impedance_ohm',
                 'detector', 'rbw_hz', 'waveform'):
        if type(sweep.settings.get(name)) is bool or sweep.settings.get(name) != profile[name]:
            raise FieldStrengthUnavailable(f'Нет подтверждённых совместимых настроек: {name}')
    indices = [i for i, f in enumerate(sweep.frequencies_hz) if f == frequency_hz]
    if len(indices) != 1:
        raise FieldStrengthUnavailable('Нужна единственная исходная точка на выбранной частоте')
    points = profile['points']
    frequencies = [p['frequency_hz'] for p in points]
    if not frequencies[0] <= frequency_hz <= frequencies[-1]:
        raise FieldStrengthUnavailable('Экстраполяция калибровки запрещена')
    right = bisect_left(frequencies, frequency_hz)
    if frequencies[right] == frequency_hz:
        coefficients = dict(points[right])
    else:
        left = right - 1
        share = (frequency_hz - frequencies[left]) / (frequencies[right] - frequencies[left])
        coefficients = {'frequency_hz': frequency_hz}
        for name in points[0].keys() - {'frequency_hz'}:
            a, b = points[left][name], points[right][name]
            coefficients[name] = None if a is None or b is None else a + share * (b - a)
    index = indices[0]
    level = sweep.levels[index]
    _number(level, 'source level')
    try:
        if is_field_unit(sweep.level_unit):
            field = convert(level, sweep.level_unit, 'dBuV/m')
        else:
            voltage = convert(level, sweep.level_unit, 'dBuV', impedance=profile['impedance_ohm'])
            field = (voltage + coefficients['antenna_factor_db_per_m']
                     + coefficients['cable_loss_db'] - coefficients['preamp_gain_db']
                     + coefficients['level_correction_db'])
        value = convert(field, 'dBuV/m', 'V/m')
    except (ValueError, OverflowError) as exc:
        raise FieldStrengthUnavailable('Исходная единица или значение не поддерживают RMS-оценку поля') from exc
    uncertainty = coefficients['uncertainty_db']
    canonical = json.dumps(profile, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return {
        'methodology': 'field_strength_rms_v2', 'value': value, 'unit': 'V/m',
        'value_dbuv_per_m': field, 'uncertainty_db': uncertainty,
        'warnings': [] if uncertainty is not None else ['Погрешность калибровки неизвестна'],
        'source': {'id': sweep.id, 'session_id': sweep.session_id, 'stream_id': sweep.stream_id,
                   'interval': to_primitive(sweep.interval), 'frequency_hz': frequency_hz,
                   'bin_index': index, 'level': level, 'level_unit': sweep.level_unit,
                   'settings': deepcopy(sweep.settings)},
        'calibration': {'profile': profile, 'sha256': hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
                        'coefficients': coefficients},
        'applicability': 'narrowband CW RMS-equivalent; operator-supplied calibration',
    }
