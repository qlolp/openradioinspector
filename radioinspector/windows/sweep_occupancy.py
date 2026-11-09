"""Validation and lossless export of the core's sampled occupancy result."""
import json
from math import isfinite
from .sweep_context import require, validate_context
from .spectral_mask import same_json
from .sweep_average import save_analysis_bytes


def finite_number(value):
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def validate_occupancy(data, expected, source, before, after, threshold):
    validate_context(data, expected, source, before, after)
    require(finite_number(threshold))
    value = data.get('occupancy')
    require(isinstance(value, dict))
    require(value.get('method') == 'sample_fraction' and value.get('comparison') == 'strictly_greater')
    require(finite_number(value.get('threshold_dbm')) and value['threshold_dbm'] == threshold)
    samples = len(data['items'])
    require(type(value.get('sample_count')) is int and value['sample_count'] == samples)
    require('rf_time_occupancy' in value and value['rf_time_occupancy'] is None)
    counts, fractions = value.get('exceedance_counts'), value.get('sample_fractions')
    require(isinstance(counts, list) and isinstance(fractions, list) and len(counts) == len(fractions) == data['points'])
    require(all(type(n) is int and 0 <= n <= samples and finite_number(f) and abs(f-n/samples) <= 1e-12 for n, f in zip(counts, fractions)))
    span, covered, gap = (value.get('acquisition_' + key + '_seconds') for key in ('span', 'covered', 'gap'))
    require(all(finite_number(v) and v >= 0 for v in (span, covered, gap)))
    require(covered <= span and abs(span-covered-gap) <= 1e-6)
    require(type(value.get('acquisition_gap_count')) is int and 0 <= value['acquisition_gap_count'] < samples)
    return data


def occupancy_text(data):
    v = data['occupancy']
    lines = [f"Доля свипов > {v['threshold_dbm']} dBm · N={v['sample_count']}",
             f"Интервал {v['acquisition_span_seconds']} с · покрытие сбора {v['acquisition_covered_seconds']} с · пропуски {v['acquisition_gap_seconds']} с ({v['acquisition_gap_count']})",
             'Это доля отсчётов, не временная занятость эфира. Недостающие свипы не считаются тишиной.']
    lines.extend(f"[{i}] {hz} Гц · {v['exceedance_counts'][i]}/{v['sample_count']} · {100*v['sample_fractions'][i]:.6g}%" for i, hz in enumerate(data['frequencies_hz']))
    return '\n'.join(lines)


def save_occupancy_file(path, data, data_json, overwrite=False):
    # Require retained wire JSON so nested settings and integers are never re-encoded.
    require(isinstance(data, dict) and isinstance(data_json, str) and same_json(json.loads(data_json), dict(data)))
    payload = ('{"format":"openradioinspector-sweep-occupancy","format_version":1,"data":' + data_json + '}\n').encode('utf-8')
    save_analysis_bytes(path, payload, overwrite)
