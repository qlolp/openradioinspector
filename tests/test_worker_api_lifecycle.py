"""Controlled HTTP lifecycle races using only in-memory synthetic sessions."""
from __future__ import annotations

import http.client
import json
import threading
import unittest
from unittest.mock import patch

from radioinspector.session_service import SessionService
from radioinspector.web.server import create_server
from tests.test_device_workers import simulated_session_payload


class WorkerStopAdmissionHttpTests(unittest.TestCase):
    def setUp(self):
        self.service = SessionService()
        self.session = self.service.create_session(simulated_session_payload())
        self.service.start_session(self.session.id)
        self.server = create_server(host='127.0.0.1', port=0, sessions=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.manager = self.server.worker_manager
        self.prefix = f'/api/v1/sessions/{self.session.id}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server.control_scheduler.close()
        self.server.automatic_completion.close()
        self.server.automatic_detection.close()
        self.server.automatic_handoff.close()
        self.manager.close()
        self.service.close()

    def post(self, suffix):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request('POST', self.prefix + suffix, body=b'{}',
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_session_stop_rejects_start_after_workers_stopped_before_state_saved(self):
        self.assertEqual(self.post('/workers/start')[0], 200)
        workers_stopped, release = threading.Event(), threading.Event()
        stopped_response = []
        original = self.manager.stop_session

        def pause_after_stop(session_id):
            result = original(session_id)
            workers_stopped.set()
            if not release.wait(4):
                raise AssertionError('Test release was not signalled')
            return result

        def request_stop():
            try:
                stopped_response.append(self.post('/stop'))
            except Exception as exc:
                stopped_response.append(exc)

        with patch.object(self.manager, 'stop_session', side_effect=pause_after_stop):
            stopping = threading.Thread(target=request_stop, daemon=True)
            stopping.start()
            try:
                self.assertTrue(workers_stopped.wait(3))
                self.assertEqual(self.manager.active_count, 0)
                self.assertEqual(self.service.get_session(self.session.id).state.value, 'running')
                status, response = self.post(f'/streams/{self.session.streams[0].id}/start')
                self.assertEqual(status, 400, response)
                self.assertEqual(response['error']['code'], 'worker_error')
                self.assertEqual(self.manager.active_count, 0)
            finally:
                release.set()
                stopping.join(timeout=5)
        self.assertFalse(stopping.is_alive())
        self.assertEqual(stopped_response[0][0], 200, stopped_response)
        self.assertEqual(self.service.get_session(self.session.id).state.value, 'stopped')
        self.assertEqual(self.manager.active_count, 0)
        self.assertTrue(all(not item['alive'] for item in self.manager.list_workers(self.session.id)))

    def test_stop_workers_allows_explicit_restart_in_running_session(self):
        self.assertEqual(self.post('/workers/start')[0], 200)
        self.assertEqual(self.post('/workers/stop')[0], 200)
        self.assertEqual(self.manager.active_count, 0)
        self.assertEqual(self.service.get_session(self.session.id).state.value, 'running')
        self.assertEqual(self.post('/workers/start')[0], 200)
        self.assertEqual(self.manager.active_count, 2)

    def test_failed_session_state_save_releases_start_admission(self):
        self.assertEqual(self.post('/workers/start')[0], 200)
        with patch.object(self.service, 'stop_session', side_effect=ValueError('Synthetic save failure')):
            self.assertEqual(self.post('/stop')[0], 400)
        self.assertEqual(self.manager.active_count, 0)
        self.assertEqual(self.service.get_session(self.session.id).state.value, 'running')
        self.assertEqual(self.post('/workers/start')[0], 200)
        self.assertEqual(self.manager.active_count, 2)


if __name__ == '__main__':
    unittest.main()
