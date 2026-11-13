"""Field estimates read archived sources, without modifying them or guard state."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_field_strength_rms import profile, source
from tests.test_sweep_comparison import create_session
from radioinspector.contracts import to_primitive


class FieldStrengthAPITests(unittest.TestCase):
    def test_memory_sqlite_and_restart_keep_raw_sources_and_event_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'archive.sqlite')
            for storage in (None, SQLiteSessionStore(path)):
                with self.subTest(sqlite=storage is not None):
                    service = SessionService(store=storage)
                    try:
                        session = create_session(service)
                        sweep = source(); sweep.session_id = session.id; sweep.stream_id = session.streams[0].id
                        service.ingest_sweep(to_primitive(sweep))
                        before = copy.deepcopy((service.health(), service.events(), service.list_sweeps(session.id)))
                        result = service.field_strength_rms_v2(session.id, {
                            'sweep_id': sweep.id, 'frequency_hz': 200, 'calibration': profile()})
                        self.assertAlmostEqual(result['value_dbuv_per_m'], 55)
                        self.assertEqual(result['source']['stream_id'], sweep.stream_id)
                        self.assertEqual(before, (service.health(), service.events(), service.list_sweeps(session.id)))
                        self.assertTrue(service.capabilities()['features']['field_strength_rms_v2'])
                    finally: service.close()
            service = SessionService(store=SQLiteSessionStore(path))
            try:
                self.assertEqual(to_primitive(service.list_sweeps(session.id)), to_primitive(before[2]))
            finally: service.close()

    def test_http_contract_and_failures_do_not_mutate_archive(self):
        service = SessionService(); session = create_session(service)
        sweep = source(); sweep.session_id = session.id; sweep.stream_id = session.streams[0].id
        service.ingest_sweep(to_primitive(sweep))
        server = create_server(host='127.0.0.1', port=0, sessions=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        before = copy.deepcopy((service.health(), service.events(), service.list_sweeps(session.id)))
        body = {'sweep_id': sweep.id, 'frequency_hz': 200, 'calibration': profile()}
        path = f'/api/v1/sessions/{session.id}/field-strength-rms-v2'
        def request(payload, suffix=''):
            conn = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                conn.request('POST', path + suffix, json.dumps(payload), {'Content-Type': 'application/json'})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally: conn.close()
        try:
            status, response = request(body)
            self.assertEqual(status, 200)
            self.assertAlmostEqual(response['data']['value_dbuv_per_m'], 55)
            for changes, expected in (({'frequency_hz': 150}, 409), ({'sweep_id': 'missing'}, 404),
                                      ({'frequency_hz': True}, 400), ({'calibration': {}}, 400)):
                with self.subTest(changes=changes):
                    self.assertEqual(request({**body, **changes})[0], expected)
            self.assertEqual(request(body, '?ignored=1')[0], 400)
            self.assertEqual(request(None)[0], 400)
            self.assertEqual(before, (service.health(), service.events(), service.list_sweeps(session.id)))
        finally:
            server.shutdown(); thread.join(3); server.server_close(); service.close()
