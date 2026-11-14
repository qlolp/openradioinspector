"""Bounded, snapshot-stable summaries; frequency filters select whole sweeps."""
from __future__ import annotations

import base64
import hashlib
import heapq
import json
from datetime import datetime

from .contracts import ensure_utc, parse_time, to_primitive
from .spectral_mask import strict_json_loads


def _time(value):
    parsed = ensure_utc(value) if isinstance(value, datetime) else parse_time(value)
    return parsed.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def query_parameters(session_id, *, stream_id=None, limit=100, after=None,
                     start=None, end=None, min_hz=None, max_hz=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Summary limit must be 1..100')
    filters = dict(session_id=session_id, stream_id=stream_id,
                   start=None if start is None else _time(start),
                   end=None if end is None else _time(end), min_hz=min_hz, max_hz=max_hz)
    if start is not None and end is not None and filters['end'] <= filters['start']:
        raise ValueError('Summary end must follow start')
    for value in (min_hz, max_hz):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('Summary frequencies must be nonnegative integer Hz')
    if min_hz is not None and max_hz is not None and max_hz < min_hz:
        raise ValueError('Summary frequency interval is reversed')
    digest = hashlib.sha256(json.dumps(filters, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode('utf-8')).hexdigest()
    cursor = None
    if after is not None:
        try:
            if not isinstance(after, str) or not 1 <= len(after) <= 16384:
                raise ValueError('Invalid summary cursor length')
            raw = base64.b64decode(after + '='*(-len(after) % 4), altchars=b'-_', validate=True)
            cursor = strict_json_loads(raw.decode('utf-8'))
            if (not isinstance(cursor, dict) or set(cursor) != {'v', 'filter', 'boundary', 'time', 'id'}
                    or type(cursor['v']) is not int or cursor['v'] != 1 or cursor['filter'] != digest
                    or type(cursor['boundary']) is not int or cursor['boundary'] < 0
                    or not isinstance(cursor['id'], str) or not cursor['id']
                    or _time(cursor['time']) != cursor['time']):
                raise ValueError('Invalid summary cursor')
            cursor['id'].encode('utf-8', errors='strict')
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
            raise ValueError('Invalid summary cursor or changed filters') from exc
    return filters, digest, cursor


def summary(sweep):
    peak = max(range(len(sweep.levels)), key=sweep.levels.__getitem__)
    return dict(id=sweep.id, session_id=sweep.session_id, stream_id=sweep.stream_id,
                interval=to_primitive(sweep.interval), level_unit=sweep.level_unit,
                point_count=len(sweep.levels), min_frequency_hz=min(sweep.frequencies_hz),
                max_frequency_hz=max(sweep.frequencies_hz),
                peak_frequency_hz=sweep.frequencies_hz[peak], peak_level=sweep.levels[peak])


def _page(items, extra, boundary, digest):
    cursor = None
    if extra and items:
        last = items[-1]
        raw = json.dumps(dict(v=1, filter=digest, boundary=boundary,
                              time=_time(last['interval']['started_at']), id=last['id']),
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        cursor = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    return dict(items=items, next_cursor=cursor)


def memory_page(entries, filters, digest, cursor, limit):
    current = entries[-1][0] if entries else 0
    boundary = cursor['boundary'] if cursor else current
    if boundary > current:
        raise ValueError('Summary cursor belongs to a different archive state')
    def eligible():
        for sequence, sweep in entries:
            if sequence > boundary:
                continue
            started = _time(sweep.interval.started_at)
            key = (started, sweep.id.encode('utf-8'))
            if cursor and key >= (cursor['time'], cursor['id'].encode('utf-8')):
                continue
            if filters['stream_id'] is not None and sweep.stream_id != filters['stream_id']:
                continue
            if filters['start'] is not None and started < filters['start']:
                continue
            if filters['end'] is not None and started >= filters['end']:
                continue
            if filters['min_hz'] is not None and max(sweep.frequencies_hz) < filters['min_hz']:
                continue
            if filters['max_hz'] is not None and min(sweep.frequencies_hz) > filters['max_hz']:
                continue
            yield (key, sweep)
    selected = heapq.nlargest(limit+1, eligible(), key=lambda item: item[0])
    return _page([summary(item) for _, item in selected[:limit]], len(selected)>limit, boundary, digest)


def sqlite_page(store, filters, digest, cursor, limit):
    with store.read_snapshot() as db:
        current = db.execute('SELECT COALESCE(MAX(sequence),0) FROM sweep_ingestion WHERE session_id=?',
                             (filters['session_id'],)).fetchone()[0]
        boundary = cursor['boundary'] if cursor else current
        if boundary > current:
            raise ValueError('Summary cursor belongs to a different archive state')
        clauses = ['m.session_id=?', 'i.sequence<=?', 's.id=m.sweep_id', 'i.sweep_id=m.sweep_id']
        args = [filters['session_id'], boundary]
        if filters['stream_id'] is not None:
            clauses.append('s.stream_id=?'); args.append(filters['stream_id'])
        for name, sql in (('start','m.started_at>=?'), ('end','m.started_at<?'),
                          ('min_hz','m.max_hz>=?'), ('max_hz','m.min_hz<=?')):
            if filters[name] is not None:
                clauses.append(sql); args.append(filters[name])
        if cursor:
            clauses.append('(m.started_at<? OR (m.started_at=? AND context_utf8(m.sweep_id)<?))')
            args.extend((cursor['time'], cursor['time'], cursor['id'].encode('utf-8')))
        # Drive the portable normalized-time index first. Only bounded IDs are
        # retained; decode one raw row at a time, never 100 large source arrays.
        rows = db.execute('''SELECT m.sweep_id FROM monitoring_sweep_index m
            INDEXED BY monitoring_sweep_index_scope_time CROSS JOIN sweeps s
            CROSS JOIN sweep_ingestion i WHERE ''' + ' AND '.join(clauses) +
            ' ORDER BY m.started_at DESC,context_utf8(m.sweep_id) DESC LIMIT ?',
            (*args, limit+1)).fetchall()
        items = []
        for row in rows[:limit]:
            source = db.execute('SELECT * FROM sweeps WHERE id=?', (row[0],)).fetchone()
            items.append(summary(store._sweep_from_row(source)))
        return _page(items, len(rows)>limit, boundary, digest)
