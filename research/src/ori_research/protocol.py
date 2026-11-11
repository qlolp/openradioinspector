"""Strict v1 input/output shapes; no user SQL, commands, URLs, or ports."""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path

REQUEST_BYTES = 65_536
RESPONSE_BYTES = 2 * 1024 * 1024
I64_MAX = 2**63 - 1
DEFAULT_LIMITS = {
    'duckdb_threads': 2,
    'duckdb_memory_bytes': 512 * 1024**2,
    'worker_rss_bytes': 768 * 1024**2,
    'cache_bytes': 10 * 1024**3,
    'temp_bytes': 10 * 1024**3,
    'io_bytes_per_second': 20 * 1024**2,
    'batch_rows': 1024,
    'batch_bytes': 4 * 1024**2,
    'max_record_bytes': 8 * 1024**2,
    'max_points_per_sweep': 65_536,
}


class WorkerError(Exception):
    def __init__(self, code, message, *, retryable=False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def _bad():
    raise WorkerError('invalid_request', 'Invalid version 1 worker request')


def _integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _string(value, maximum):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= maximum \
        and all(ord(c) >= 32 for c in value)


def _path(value):
    return _string(value, 4096) and Path(value).is_absolute()


def parse(raw):
    try:
        if len(raw) > REQUEST_BYTES:
            _bad()
        def reject_constant(_):
            _bad()
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    _bad()
                result[key] = value
            return result
        request = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
        allowed = {'version', 'request_id', 'operation', 'snapshot_path', 'cache_directory',
                   'filters', 'limits', 'page_size', 'cursor', 'threshold'}
        if not isinstance(request, dict) or set(request) - allowed:
            _bad()
        if type(request.get('version')) is not int or request['version'] != 1 \
                or not _string(request.get('request_id'), 128) \
                or request.get('operation') not in ('export', 'summary', 'occupancy') \
                or not _path(request.get('snapshot_path')) \
                or not _path(request.get('cache_directory')):
            _bad()
        filters = request.setdefault('filters', {})
        keys = {'session_id', 'stream_id', 'started_at_us', 'ended_at_us',
                'min_frequency_hz', 'max_frequency_hz'}
        if not isinstance(filters, dict) or set(filters) - keys:
            _bad()
        for key, value in filters.items():
            if key.endswith('_id'):
                if not _string(value, 1024):
                    _bad()
            elif not _integer(value, -(2**63) if key.endswith('_us') else 0, I64_MAX):
                _bad()
        if filters.get('started_at_us', -(2**63)) >= filters.get('ended_at_us', I64_MAX):
            _bad()
        if filters.get('min_frequency_hz', 0) > filters.get('max_frequency_hz', I64_MAX):
            _bad()
        if not _integer(request.setdefault('page_size', 100), 1, 500):
            _bad()
        if 'cursor' in request and not _string(request['cursor'], 4096):
            _bad()
        if request['operation'] == 'export' and 'cursor' in request:
            _bad()
        limits = request.setdefault('limits', {})
        if not isinstance(limits, dict) or set(limits) - DEFAULT_LIMITS.keys():
            _bad()
        for key, value in limits.items():
            if not _integer(value, 1, DEFAULT_LIMITS[key]):
                _bad()
        request['limits'] = DEFAULT_LIMITS | limits
        if request['operation'] == 'occupancy':
            threshold = request.get('threshold')
            if not isinstance(threshold, dict) or set(threshold) != {'value', 'unit'} \
                    or type(threshold['value']) not in (int, float) \
                    or not math.isfinite(threshold['value']) or not _string(threshold['unit'], 128):
                _bad()
            threshold['value'] = float(threshold['value'])
        elif 'threshold' in request:
            _bad()
        return request
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        _bad()


def cursor_context(request, cache_key):
    return fingerprint({'cache_key': cache_key, 'operation': request['operation'],
                        'threshold': request.get('threshold'), 'page_size': request['page_size']})


def read_cursor(request, cache_key):
    value = request.get('cursor')
    if value is None:
        return None
    try:
        raw = base64.b64decode(value, altchars=b'-_', validate=True)
        decoded = json.loads(raw)
        last = decoded['last']
        count = 5 if request['operation'] == 'occupancy' else 4
        if set(decoded) != {'context', 'last'} or decoded['context'] != cursor_context(request, cache_key) \
                or not isinstance(last, list) or len(last) != count \
                or not all(_string(item, 1024) for item in last[:4]) \
                or last[0] not in ('sweep', 'observation') \
                or (count == 5 and not _integer(last[4], 0, I64_MAX)):
            raise ValueError()
        return last
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise WorkerError('invalid_cursor', 'Cursor does not match this query') from None


def make_cursor(request, cache_key, last):
    return base64.urlsafe_b64encode(encode({'context': cursor_context(request, cache_key),
                                          'last': last})).decode('ascii')
