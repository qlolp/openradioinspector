"""Actual archived bins, not interpolation, RF occupancy or per-bin timestamps."""
from __future__ import annotations

import hashlib
import json

from .contracts import to_primitive
from .sweep_comparison import SWEEP_METADATA_KEYS
from .sweep_settings import json_value
from .sweep_references import normalize_reference_name
from .sweep_summaries import _time

MAX_HZ = 9_007_199_254_740_991
MAX_BOOKMARK_VERSIONS = 256


def frequency(value):
    if type(value) is not int or not 0 <= value <= MAX_HZ:
        raise ValueError('Частота должна быть целым числом Гц')
    return value


def history_request(value):
    required = {'stream_id', 'frequencies_hz', 'start', 'end'}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'limit', 'after'}:
        raise ValueError('Нужны stream_id, frequencies_hz, start, end; допустимы limit и after')
    if not isinstance(value['stream_id'], str) or not 1 <= len(value['stream_id']) <= 128:
        raise ValueError('Некорректный поток')
    frequencies = value['frequencies_hz']
    if not isinstance(frequencies, list) or not 1 <= len(frequencies) <= 4:
        raise ValueError('Выберите от одной до четырёх частот')
    for item in frequencies: frequency(item)
    if len(set(frequencies)) != len(frequencies):
        raise ValueError('Частоты не должны повторяться')
    if not isinstance(value['start'], str) or not isinstance(value['end'], str):
        raise ValueError('Требуется интервал UTC')
    return dict(stream_id=value['stream_id'], start=_time(value['start']), end=_time(value['end']),
                limit=value.get('limit',100), after=value.get('after'),
                context={'frequency_history_v1':list(frequencies)}), list(frequencies)


def history_item(sweep, frequencies):
    settings = sweep.settings.copy()
    for key in SWEEP_METADATA_KEYS:
        if key in settings and not json_value(settings.pop(key), nullable=True):
            raise ValueError('Метаданные свипа должны содержать только значения JSON')
    context = dict(stream=sweep.stream_id, unit=sweep.level_unit, grid=sweep.frequencies_hz, settings=settings)
    if not settings:
        context['unknown_configuration'] = sweep.id
    digest = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False,
        separators=(',',':'), allow_nan=False).encode('utf-8')).hexdigest()
    samples = []
    for requested in frequencies:
        index = None
        if sweep.frequencies_hz and min(sweep.frequencies_hz) <= requested <= max(sweep.frequencies_hz):
            index = min(range(len(sweep.frequencies_hz)),
                key=lambda i:(abs(sweep.frequencies_hz[i]-requested),sweep.frequencies_hz[i],i))
        samples.append(dict(requested_hz=requested,
            frequency_hz=None if index is None else sweep.frequencies_hz[index],
            level=None if index is None else sweep.levels[index]))
    return dict(sweep_id=sweep.id, interval=to_primitive(sweep.interval), level_unit=sweep.level_unit,
                segment_id=digest, samples=samples)


def bookmark_request(value):
    if not isinstance(value,dict) or set(value) != {'name','entries'}:
        raise ValueError('Набор частот содержит только name и entries')
    name = normalize_reference_name(value['name'])
    entries = value['entries']
    if not isinstance(entries,list) or not 1 <= len(entries) <= 4:
        raise ValueError('В наборе должно быть 1–4 частоты')
    result = []
    for item in entries:
        if not isinstance(item,dict) or set(item) != {'label','frequency_hz'}:
            raise ValueError('Метка содержит только label и frequency_hz')
        result.append(dict(label=normalize_reference_name(item['label']), frequency_hz=frequency(item['frequency_hz'])))
    if len({item['frequency_hz'] for item in result}) != len(result):
        raise ValueError('Частоты набора не должны повторяться')
    return dict(name=name,entries=result)
