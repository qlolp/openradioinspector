"""Offline, verified CSV summary of a version-1 occupancy JSON artifact."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from .contracts import Sweep, parse_time
from .sweep_context import calculate_sweep_context, sweep_order_key, validate_context_request
from .sweep_occupancy import summarize_occupancy, validate_occupancy_request

MAX_ARTIFACT_BYTES = 2_100_000
MAX_CSV_BYTES = 16 * 1024 * 1024


class _CSVBuffer(io.StringIO):
    """Bound repeated provenance fields as well as the input measurements."""
    def __init__(self):
        super().__init__(newline='')
        self.byte_count = 0

    def write(self, text):
        self.byte_count += len(text.encode('utf-8'))
        if self.byte_count > MAX_CSV_BYTES:
            raise ValueError('CSV превышает 16 MiB; используйте исходный JSON')
        return super().write(text)


def _require(condition):
    if not condition:
        raise ValueError('Некорректный или несогласованный артефакт занятости')


def _equal(a, b):
    if type(a) in (int, float) and type(b) in (int, float):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def parse_artifact(raw: bytes) -> dict:
    """Revalidate the window and recompute the summary using the shared core."""
    _require(len(raw) <= MAX_ARTIFACT_BYTES)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result
    def reject(value):
        raise ValueError('Неконечное число JSON')
    try:
        artifact = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=reject)
        _require(isinstance(artifact, dict) and set(artifact) == {'format', 'format_version', 'data'})
        _require(artifact['format'] == 'openradioinspector-sweep-occupancy')
        _require(type(artifact['format_version']) is int and artifact['format_version'] == 1)
        data = artifact['data']
        _require(isinstance(data, dict))
        json.dumps(data, allow_nan=False, ensure_ascii=False).encode('utf-8')
        request = dict(sweep_id=data['anchor_id'], **data['requested'])
        anchor_id, before, after = validate_context_request(request)
        _, threshold = validate_occupancy_request({**request, 'threshold_dbm': data['occupancy']['threshold_dbm']})
        _require(all(isinstance(data[k], str) and data[k].strip() for k in ('session_id', 'stream_id')))
        items = data['items']; grid = data['frequencies_hz']
        _require(type(data['points']) is int)
        for name in ('available', 'missing'):
            _require(isinstance(data[name], dict) and set(data[name]) == {'before', 'after'})
            _require(all(type(v) is int for v in data[name].values()))
        summary = data['occupancy']
        _require(type(summary['sample_count']) is int and type(summary['acquisition_gap_count']) is int)
        _require(isinstance(summary['exceedance_counts'], list) and all(type(v) is int for v in summary['exceedance_counts']))
        _require(isinstance(items, list) and 1 <= len(items) <= 33)
        _require(isinstance(grid, list) and 1 <= len(grid) <= 16384 and len(grid)*len(items) <= 131072)
        _require(all(type(f) is int and 0 <= f <= 9007199254740991 for f in grid))
        sweeps = []
        for item in items:
            _require(isinstance(item, dict) and isinstance(item['id'], str) and type(item['offset']) is int)
            _require(isinstance(item['levels'], list) and all(type(v) in (int, float) and math.isfinite(v) for v in item['levels']))
            sweeps.append(Sweep.from_dict(dict(item, session_id=data['session_id'], stream_id=data['stream_id'], frequencies_hz=grid)))
        ids = [s.id for s in sweeps]
        _require(len(set(ids)) == len(ids) and ids.count(anchor_id) == 1)
        _require(all(sweep_order_key(a) < sweep_order_key(b) for a, b in zip(sweeps, sweeps[1:])))
        pivot = ids.index(anchor_id)
        _require(pivot <= before and len(items)-pivot-1 <= after)
        expected = calculate_sweep_context(data['session_id'], sweeps[pivot], sweeps[:pivot], sweeps[pivot+1:], before=before, after=after)
        expected['occupancy'] = summarize_occupancy(expected, threshold)
        _require(_equal(data, expected))
        return data
    except (KeyError, TypeError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError('Некорректный артефакт занятости') from exc


def export_csv(raw: bytes) -> str:
    data = parse_artifact(raw)
    summary = data['occupancy']
    out = _CSVBuffer()
    writer = csv.writer(out, lineterminator='\r\n')
    # IDs are JSON strings/arrays, so spreadsheet cells never begin with a formula.
    writer.writerow(['format_version', 'artifact_sha256', 'session_id_json', 'stream_id_json', 'anchor_id_json',
                     'source_ids_json', 'started_at_utc', 'ended_at_utc', 'method', 'comparison', 'threshold_dbm', 'bin_index', 'frequency_hz',
                     'exceedance_count', 'sample_count', 'sample_fraction', 'complete', 'missing_before', 'missing_after',
                     'acquisition_span_seconds', 'acquisition_covered_seconds', 'acquisition_gap_seconds', 'acquisition_gap_count',
                     'rf_time_occupancy'])
    encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    prefix = [1, hashlib.sha256(raw).hexdigest(), *(encoded(data[k]) for k in ('session_id', 'stream_id', 'anchor_id')),
              encoded([item['id'] for item in data['items']]),
              min((item['interval']['started_at'] for item in data['items']), key=parse_time),
              max((item['interval']['ended_at'] for item in data['items']), key=parse_time),
              summary['method'], summary['comparison'], summary['threshold_dbm']]
    for index, hz in enumerate(data['frequencies_hz']):
        writer.writerow(prefix + [index, hz, summary['exceedance_counts'][index], summary['sample_count'], summary['sample_fractions'][index],
                                  str(data['complete']).lower(), data['missing']['before'], data['missing']['after'],
                                  summary['acquisition_span_seconds'], summary['acquisition_covered_seconds'], summary['acquisition_gap_seconds'],
                                  summary['acquisition_gap_count'], 'unknown'])
    return out.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Проверить JSON занятости и сохранить CSV-сводку без доступа к прибору')
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='новый CSV-файл; существующие файлы не заменяются')
    args = parser.parse_args(argv)
    temporary = None
    try:
        with args.input.open('rb') as source:
            raw = source.read(MAX_ARTIFACT_BYTES + 1)
        payload = export_csv(raw).encode('utf-8')
        with tempfile.NamedTemporaryFile(dir=args.output.parent, prefix='.occupancy-', delete=False) as target:
            temporary = target.name
            target.write(payload); target.flush(); os.fsync(target.fileno())
        os.link(temporary, args.output)
    except (OSError, ValueError) as exc:
        print(f'Ошибка экспорта: {exc}', file=sys.stderr)
        return 2
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
