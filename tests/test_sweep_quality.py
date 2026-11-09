"""Synthetic archived review only; no instrument commands or physical data."""
from __future__ import annotations

import copy
from contextlib import closing
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from radioinspector.contracts import Sweep
from radioinspector.session_service import SessionService, SweepNotFoundError
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.sweep_quality import (
    QualityIncompatibleError, calculate_sweep_quality, validate_quality_request,
)
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session, measurement


def request(**changes):
    return dict(sweep_id='sweep-0', edge_margin_db=20, high_level_dbm=0, **changes)


def source(levels=(-10, -70, -60, -5), **changes):
    body = measurement(levels=list(levels), frequencies_hz=list(range(100, 100 + len(levels))))
    body.update(changes)
    return Sweep.from_dict(body)


def review(sweep, **changes):
    parameters = dict(edge_margin_db=20, high_level_dbm=0)
    parameters.update(changes)
    return calculate_sweep_quality('session', sweep, **parameters)


class QualityContractTests(unittest.TestCase):
    def test_request_exact_shape_finite_numbers_and_safe_ids(self):
        invalid = [None, [], {}, dict(request(), extra=1)]
        for field in request():
            body = request(); del body[field]; invalid.append(body)
        for field, values in (
            ('sweep_id', ['', ' ', 3, True, None, 'x' * 129, '\ud800']),
            ('edge_margin_db', [0, -1, 201, True, '20', None, float('nan'), float('inf'), 10**400]),
            ('high_level_dbm', [-201, 101, True, '0', None, float('nan'), float('-inf'), 10**400]),
        ):
            for value in values:
                body = request(); body[field] = value; invalid.append(body)
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_quality_request(body)
        for edge, high in ((0.01, -200), (200, 100)):
            body = dict(sweep_id='x', edge_margin_db=edge, high_level_dbm=high)
            self.assertEqual(validate_quality_request(body), body)

    def test_signed_edges_user_threshold_not_hardware_diagnosis(self):
        original = source()
        before = copy.deepcopy(original)
        result = review(original, high_level_dbm=-10)
        self.assertEqual(result['flags'], ['first_edge_jump', 'last_edge_jump', 'level_threshold'])
        self.assertEqual(result['high_level_count'], 2)
        self.assertEqual([e['delta_db'] for e in result['edge_assessments']], [60, 55])
        self.assertEqual(result['peak_index'], 3)
        self.assertEqual(result['peak_frequency_hz'], 103)
        self.assertEqual(result['assessment'], 'review')
        self.assertFalse(result['hardware_diagnosis'])
        self.assertFalse(result['raw_modified'])
        self.assertEqual(original, before)
        result['sweep']['settings']['sequence'] = 999
        self.assertEqual(original, before)

    def test_exact_boundaries_negative_jumps_ties_two_bins_and_no_flags(self):
        result = review(source((-40, -60)), edge_margin_db=20, high_level_dbm=-40)
        self.assertEqual(result['flags'], ['first_edge_jump', 'level_threshold'])
        self.assertEqual(result['edge_assessments'][1]['delta_db'], -20)
        result = review(source((-60, -60, -60)))
        self.assertEqual(result['peak_index'], 0)
        self.assertEqual(result['flags'], [])
        self.assertEqual(result['assessment'], 'no_flags')
        # A strong interior peak is not an edge artifact and remains untouched.
        result = review(source((-90, -20, -90)))
        self.assertEqual(result['flags'], [])
        self.assertEqual(result['peak_index'], 1)

    def test_invalid_sources_and_nonrepresentable_derived_delta(self):
        mutations = {
            'level_unit': ['S'], 'session_id': ['another'],
            'frequencies_hz': [(100,), (100, 100, 102, 103), (100, 99, 102, 103),
                               (False, 101, 102, 103), (-1, 101, 102, 103),
                               (100, 101, 102, 2**53), tuple(range(16385))],
            'levels': [(-10,), (True, -1, -2, -3), (float('nan'), -1, -2, -3),
                       (float('inf'), -1, -2, -3), (1e308, -1e308, -1, -2)],
            'settings': [{'bad': float('nan')}, {'bad': '\ud800'}, {'huge': 'x' * 65536},
                         {'huge': 10**400}, {1: 'not string key'}, {'tuple': (1, 2)}],
            'stream_id': ['', 'bad\ud800'],
        }
        for field, values in mutations.items():
            for value in values:
                item = source(); setattr(item, field, value)
                with self.subTest(field=field), self.assertRaises(QualityIncompatibleError):
                    review(item)
        item = source(); item.interval.ended_at = item.interval.started_at.replace(year=2000)
        with self.assertRaises(QualityIncompatibleError): review(item)

    def test_scope_ids_preserve_existing_length_contract_but_require_utf8(self):
        item = source(stream_id='s' * 256)
        self.assertEqual(review(item)['stream_id'], 's' * 256)
        item.session_id = 'bad\ud800'
        with self.assertRaises(QualityIncompatibleError):
            calculate_sweep_quality(item.session_id, item, edge_margin_db=20, high_level_dbm=0)

    def test_old_and_new_metadata_with_unknown_readback_remain_readable(self):
        for settings in ({}, {'rbw_hz': 10000}, {'rbw_applied_hz': None, 'readback': False},
                         {'receive_profile': 'example', 'device_identity': {'firmware': 'synthetic'}}):
            item = source(settings=settings)
            self.assertEqual(review(item)['sweep']['settings'], settings)
        result = review(source(tuple([-90] * 16384)))
        self.assertEqual(result['points'], 16384)


def logical_archive_dump(path):
    # sqlite3.Connection.__exit__ commits/rolls back, but does not close the
    # handle. Keep its lifetime explicit so Windows can remove the temp DB.
    with closing(sqlite3.connect(path)) as connection:
        return '\n'.join(connection.iterdump())


class QualityDumpOwnershipTests(unittest.TestCase):
    def test_dump_closes_connection_on_success_and_iteration_failure(self):
        for failing in (False, True):
            with self.subTest(failing=failing):
                connection = MagicMock()
                connection.__enter__.return_value = connection
                if failing:
                    def broken_dump():
                        yield 'BEGIN TRANSACTION;'
                        raise RuntimeError('synthetic dump failure')
                    connection.iterdump.return_value = broken_dump()
                else:
                    connection.iterdump.return_value = iter(['BEGIN TRANSACTION;', 'COMMIT;'])
                with patch('tests.test_sweep_quality.sqlite3.connect', return_value=connection):
                    if failing:
                        with self.assertRaisesRegex(RuntimeError, 'synthetic dump failure'):
                            logical_archive_dump('synthetic-only')
                    else:
                        self.assertEqual(logical_archive_dump('synthetic-only'), 'BEGIN TRANSACTION;\nCOMMIT;')
                connection.close.assert_called_once_with()



class QualityPersistenceTests(unittest.TestCase):
    def test_read_only_restart_logical_archive_and_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'archive.sqlite')
            service = SessionService(store=SQLiteSessionStore(path))
            session = create_session(service)
            service.ingest_sweep(measurement(session.id, session.streams[0].id, 0,
                                             levels=[-5, -70, -60, -55]))
            before = (service.events(), service.list_sweeps(session.id), service.list_sessions())
            first = service.sweep_quality(session.id, request())
            with self.assertRaises(SweepNotFoundError):
                service.sweep_quality(session.id, dict(request(), sweep_id='missing'))
            self.assertEqual(before, (service.events(), service.list_sweeps(session.id), service.list_sessions()))
            service.close()
            baseline = logical_archive_dump(path)
            service = SessionService(store=SQLiteSessionStore(path))
            try:
                self.assertEqual(service.sweep_quality(session.id, request()), first)
            finally:
                service.close()
            self.assertEqual(logical_archive_dump(path), baseline)


class QualityHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / 'archive.sqlite')
        service = SessionService(store=SQLiteSessionStore(path))
        self.session = create_session(service)
        service.ingest_sweep(measurement(self.session.id, self.session.streams[0].id, 0,
                                         levels=[-5, -70, -60, -55]))
        # Duplicate frequency source can be archived but is incompatible with spatial review.
        service.ingest_sweep(measurement(self.session.id, self.session.streams[0].id, 1,
                                         frequencies_hz=[100, 100, 300, 400]))
        service.close()
        self.server = create_server(host='127.0.0.1', port=0, session_database=path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop)
        self.root = f'/api/v1/sessions/{self.session.id}/sweep-quality'

    def stop(self):
        self.server.shutdown(); self.thread.join(timeout=3)
        for name in ('automatic_completion', 'automatic_detection', 'automatic_handoff',
                     'control_scheduler', 'worker_manager', 'session_service'):
            getattr(self.server, name).close()
        self.server.server_close()

    def post(self, body, *, path=None, raw=False, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request('POST', path or self.root, body if raw else json.dumps(body).encode(),
                               headers=headers or {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_report_errors_capability_and_no_mutation(self):
        before = self.server.session_service.list_sweeps(self.session.id)
        code, envelope = self.post(request())
        self.assertEqual(code, 200)
        self.assertEqual(envelope['data']['flags'], ['first_edge_jump'])
        self.assertTrue(self.server.session_service.capabilities()['features']['sweep_quality'])
        for body, code, error in (
            (dict(request(), sweep_id='absent'), 404, 'sweep_not_found'),
            (dict(request(), sweep_id='sweep-1'), 409, 'quality_incompatible'),
            ({'sweep_id': 'x'}, 400, 'invalid_request'),
        ):
            actual, response = self.post(body)
            self.assertEqual((actual, response['error']['code']), (code, error))
        code, _ = self.post(request(), path='/api/v1/sessions/absent/sweep-quality')
        self.assertEqual(code, 404)
        self.assertEqual(self.server.session_service.list_sweeps(self.session.id), before)

    def test_bounded_strict_json_origin_and_query(self):
        for body, code in ((b'{"sweep_id":"x","sweep_id":"y"}', 400),
                           (b'{"edge_margin_db":NaN}', 400), (b'{' + b' ' * 8193, 413)):
            actual, _ = self.post(body, raw=True)
            self.assertEqual(actual, code)
        self.assertEqual(self.post(request(), path=self.root + '?x=1')[0], 400)
        self.assertEqual(self.post(request(), headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.post(request(), headers={'Content-Type': 'application/json',
                                                       'Origin': 'https://example.invalid'})[0], 403)
