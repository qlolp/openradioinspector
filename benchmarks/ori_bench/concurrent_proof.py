"""Bounded evidence for a live snapshot; never changes the producer's ledger."""
from __future__ import annotations

import hashlib

from .config import BenchmarkError, Config
from .source import identifier, measurement_hash, observation, sweep
from tools.modernization.api_parity import load_json

MAX_EXPORT_BYTES = 8 * 1024**3
MAX_FRAME_BYTES = 16 * 1024**2


def reference_config(origin, output, run_id, **options):
    return Config(base_url=origin, output_dir=output, run_id=run_id,
                  observe_render=True, observer_clients=3, **options)


def invalid(message='Live snapshot lost, repeated, or changed an original source'):
    return BenchmarkError('background_export_invalid', message)


class ExportProof:
    def __init__(self, config, headers, *, max_bytes=MAX_EXPORT_BYTES,
                 max_frame_bytes=MAX_FRAME_BYTES):
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EXPORT_BYTES \
                or type(max_frame_bytes) is not int or not 1 <= max_frame_bytes <= MAX_FRAME_BYTES:
            raise invalid('Export budgets must be positive lowering-only byte bounds')
        self.config, self.max_bytes, self.max_frame_bytes = config, max_bytes, max_frame_bytes
        def number(key):
            text = headers.get(key)
            if not isinstance(text, str) or len(text) > 20 or not text.isascii() or not text.isdecimal() \
                    or str(int(text)) != text or int(text) >= 2**64:
                raise invalid('Live snapshot boundary/count headers are missing or invalid')
            return int(text)
        self.expected = dict(sweep=number('X-Archive-Sweeps'),
                             observation=number('X-Archive-Observations'))
        if any(count > config.planned_counts()[kind] for kind, count in self.expected.items()):
            raise invalid('Live snapshot contains more than the owned offered source bound')
        self.boundary = number('X-Archive-Boundary')
        self.counts = dict(sweep=0, observation=0)
        self.seen, self.digest = set(), hashlib.sha256()
        self.bytes = self.largest_frame = self.records = 0
        self.header = False

    def observe(self, raw):
        self.bytes += len(raw)
        self.largest_frame = max(self.largest_frame, len(raw))
        if self.bytes > self.max_bytes or len(raw) > self.max_frame_bytes or not raw.endswith(b'\n'):
            raise invalid('Live snapshot exceeded its total/frame bound or ended in a partial frame')
        try:
            value = load_json(raw, self.max_frame_bytes)
            if not self.header:
                if not isinstance(value, dict) or type(value.get('format_version')) is not int \
                        or value != dict(format='openradioinspector-archive-ndjson', format_version=1,
                                 session_id=self.config.session_id):
                    raise ValueError('snapshot header')
                self.header = True
            else:
                if not isinstance(value, dict) or set(value) != {'kind', 'measurement'}:
                    raise ValueError('snapshot record')
                kind, item = value['kind'], value['measurement']
                if kind not in self.counts or not isinstance(item, dict):
                    raise ValueError('source kind')
                if not isinstance(item.get('id'), str):
                    raise ValueError('source ID')
                index = int(item['id'].rsplit('-', 1)[1])
                if index < 0 or index >= self.config.planned_counts()[kind] \
                        or item['id'] != identifier(self.config, kind, index) \
                        or (kind, index) in self.seen or self.counts[kind] >= self.expected[kind]:
                    raise ValueError('source identity')
                expected = (sweep if kind == 'sweep' else observation)(self.config, index)
                if measurement_hash(kind, item) != measurement_hash(kind, expected):
                    raise ValueError('original source bytes')
                self.seen.add((kind, index))
                self.counts[kind] += 1
                self.records += 1
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError) as error:
            raise invalid() from error
        self.digest.update(raw)

    def finish(self):
        if not self.header or self.counts != self.expected:
            raise invalid('Live snapshot ended before all of its declared source records')
        return dict(status='passed', source_boundary=self.boundary, source_counts=self.counts,
                    ndjson_sha256=self.digest.hexdigest(), ndjson_bytes=self.bytes,
                    maximum_frame_bytes=self.largest_frame, all_planned_inputs_claimed=False)


def overlap(backup, raw, history, qos):
    """Require observed active owners, not merely three submitted requests."""
    return (backup.get('phase') == 'running' and raw.get('phase') == 'streaming'
            and type(raw.get('verified_records')) is int and raw['verified_records'] > 0
            and history.get('phase') == 'running'
            and type(qos.get('registered_work')) is int and qos['registered_work'] >= 3
            and qos.get('cleanup_failed') is False and qos.get('acquisition_pressure') is False
            and qos.get('cross_process_logical_io') is True)
