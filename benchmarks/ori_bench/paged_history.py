"""Measure live archive pagination while the original export, backup and producer run."""
from __future__ import annotations

import sqlite3
import time
from urllib.parse import urlencode

from .concurrent import BackgroundWork, utc
from .config import BenchmarkError
from .source import identifier, sweep
from .verify import verify_sweep_summary

PAGE_SIZE = 10


def verify_page(config, value, previous_index=None):
    try:
        rows = value['items']
        cursor = value['next_cursor']
        if not isinstance(rows, list) or not 1 <= len(rows) <= PAGE_SIZE:
            raise ValueError()
        if cursor is not None and (not isinstance(cursor, str) or not 0 < len(cursor) <= 16384):
            raise ValueError()
        indices = []
        for row in rows:
            index = int(row['id'].rsplit('-', 1)[1])
            if not 0 <= index < config.planned_counts()['sweep'] or row['id'] != identifier(config, 'sweep', index):
                raise ValueError()
            if 'levels' in row or 'frequencies_hz' in row or (previous_index is not None and index >= previous_index):
                raise ValueError()
            verify_sweep_summary(config, row, sweep(config, index))
            indices.append(index)
            previous_index = index
    except (ValueError, TypeError, OverflowError, IndexError, KeyError) as error:
        raise BenchmarkError('history_invalid', 'Cursor page changed a source summary or its newest-first order') from error
    return indices, cursor


def active(snapshot):
    qos, raw = snapshot['qos'], snapshot['export']
    return (snapshot['backup'].get('phase') == 'running' and raw.get('phase') == 'streaming'
            and type(raw.get('verified_records')) is int and raw['verified_records'] > 0
            and type(qos.get('registered_work')) is int and qos['registered_work'] >= 2
            and qos.get('cleanup_failed') is False and qos.get('acquisition_pressure') is False)


class PagedHistoryWork(BackgroundWork):
    def snapshot(self):
        with self.lock:
            raw = dict(self.raw)
        return dict(export=raw, backup=self.node.backup_state(), qos=self.qos())

    def _history_work(self, deadline):
        route = self.client.session_path(self.config) + '/sweep-summaries?'
        cursor = last_index = None
        history = dict(kind='cursor_paged_sweep_summaries', pages=0, cursor_pages=0, verified_summaries=0)
        self.report['history'] = history
        while not self.cancelled.is_set() and time.monotonic() < deadline:
            before = self.snapshot()
            params = dict(stream_id=self.config.spectrum_id, limit=PAGE_SIZE)
            if cursor is not None:
                params['after'] = cursor
            start = time.monotonic_ns()
            value = self.client.json(route + urlencode(params))
            end = time.monotonic_ns()
            indices, next_cursor = verify_page(self.config, value, last_index)
            if next_cursor is not None and next_cursor == cursor:
                raise BenchmarkError('history_invalid', 'Original archive cursor did not advance')
            after = self.snapshot()
            history['pages'] += 1
            history['verified_summaries'] += len(indices)
            history['cursor_pages'] += int(cursor is not None)
            if (self.report['overlap'] is None and cursor is not None and active(before) and active(after)):
                self.report['overlap'] = dict(observed_at=utc(), started_monotonic_ns=start,
                    finished_monotonic_ns=end, verified_indices=indices, before=before, after=after)
            raw, backup = after['export'], after['backup']
            if raw['phase'] == 'failed' or backup.get('phase') == 'failed':
                raise BenchmarkError('background_failed', 'An original live export or online backup failed')
            if raw['phase'] == 'completed' and backup.get('phase') == 'completed':
                self.report.update(backup=backup, live_export=raw, finished_at=utc(),
                                   health_after=self.client.json('/api/v1/health'))
                if self.report['overlap'] is None:
                    raise BenchmarkError('background_overlap_unmeasured', 'No verified cursor page bracketed by active export and backup')
                self.report['status'] = 'passed'
                return
            cursor, last_index = next_cursor, indices[-1] if next_cursor is not None else None
            self.cancelled.wait(.05)
        raise BenchmarkError('background_incomplete', 'Concurrent archive pagination exceeded its deadline')

    def check_production_overlap(self, ledger):
        overlap = self.report['overlap']
        db = sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            first, last = db.execute("SELECT MIN(offered_ns), MAX(offered_ns) FROM offers "
                                     "WHERE kind='sweep' AND verified=1 AND status='accepted'").fetchone()
        finally:
            db.close()
        if first is None or not first <= overlap['started_monotonic_ns'] <= overlap['finished_monotonic_ns'] <= last:
            self.report['status'] = 'failed'
            raise BenchmarkError('background_overlap_unmeasured', 'Verified sweep offers did not bracket the observed page')
        overlap['verified_offers_bracket_ns'] = [first, last]
