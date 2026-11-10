import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from radioinspector.monitoring.service import MonitoringService
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.web.server import create_server
from tests.test_device_workers import simulated_session_payload


class MonitoringProjectTests(unittest.TestCase):
    def test_persistence_and_preflight_never_start_devices(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'archive.sqlite')
            store = SQLiteSessionStore(path)
            sessions = SessionService(store=store)
            workers = DeviceWorkerManager(sessions)
            service = MonitoringService(sessions, workers)
            session = sessions.create_session(simulated_session_payload())
            project = service.create(dict(name='Объект', site='Город', session_id=session.id))
            result = service.preflight(project['id'])
            self.assertTrue(result['ready'])
            self.assertFalse(result['baseline_ready'])
            self.assertEqual(result['performance']['sweeps'], 0)
            self.assertEqual(workers.active_count, 0)
            self.assertEqual(sessions.list_sweeps(session.id), [])
            service.close(); workers.close(); sessions.close(); store.close()
            reopened = SQLiteSessionStore(path)
            again = SessionService(store=reopened)
            manager = DeviceWorkerManager(again)
            recovered = MonitoringService(again, manager)
            self.assertEqual(recovered.project(project['id']), project)
            self.assertFalse(project['autostart'])
            recovered.close(); manager.close(); again.close(); reopened.close()

    def test_invalid_project_does_not_write_or_open_ports(self):
        sessions = SessionService(); workers = DeviceWorkerManager(sessions)
        service = MonitoringService(sessions, workers)
        self.addCleanup(sessions.close); self.addCleanup(workers.close); self.addCleanup(service.close)
        session = sessions.create_session({'name': 'empty'})
        with self.assertRaises(ValueError):
            service.create(dict(name='test', site='test', session_id=session.id))
        self.assertEqual(service.store.list('project'), [])
        self.assertEqual(workers.active_count, 0)

    def test_http_contract_and_old_api_survive(self):
        server = create_server(host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            session = server.session_service.create_session(simulated_session_payload())
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('POST', '/api/v1/monitoring-projects', json.dumps(dict(name='Room', site='Office', session_id=session.id)), {'Content-Type':'application/json'})
            response = connection.getresponse(); self.assertEqual(response.status, 201)
            project = json.loads(response.read())['data']; connection.close()
            for path in ['/api/v1/monitoring-projects', '/api/v1/monitoring-projects/' + project['id'] + '/preflight', '/api/v1/health']:
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                connection.request('GET', path); response = connection.getresponse()
                self.assertEqual(response.status, 200); self.assertEqual(json.loads(response.read())['contract_version'], 1); connection.close()
            self.assertEqual(server.worker_manager.active_count, 0)
        finally:
            server.shutdown(); server.server_close(); thread.join()


class MonitoringSerialReadinessTests(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionService()
        self.workers = DeviceWorkerManager(self.sessions)
        self.service = MonitoringService(self.sessions, self.workers)
        self.addCleanup(self.sessions.close)
        self.addCleanup(self.workers.close)
        self.addCleanup(self.service.close)

    def preflight(self, change=lambda payload: None):
        from unittest.mock import patch
        payload = simulated_session_payload()
        for stream, adapter, port in zip(payload['streams'],
                                        ('tinysa-serial', 'ardv1-serial'), ('COM3', 'COM4')):
            stream['settings']['adapter'] = adapter
            stream['port'] = port
        change(payload)
        session = self.sessions.create_session(payload)
        with patch('radioinspector.serial_instruments.PosixSerialTransport',
                   side_effect=AssertionError('must not create a transport')), \
             patch('radioinspector.serial_instruments.WindowsSerialTransport',
                   side_effect=AssertionError('must not create a transport')), \
             patch('radioinspector.serial_instruments.create_serial_transport',
                   side_effect=AssertionError('must not open ports')), \
             patch('radioinspector.serial_instruments.os.open',
                   side_effect=AssertionError('must not access devices')):
            project = self.service.create(dict(name='Readiness', site='Lab', session_id=session.id))
            result = self.service.preflight(project['id'])
        self.assertEqual(self.workers.active_count, 0)
        self.assertEqual(self.sessions.list_sweeps(session.id), [])
        self.assertEqual(result['hardware_validation'], 'not_run')
        return result

    def test_serial_ports_are_required_for_both_streams(self):
        for index in (0, 1):
            for port in (None, '', '   ', 'sim://device'):
                with self.subTest(index=index, port=port):
                    result = self.preflight(lambda p: p['streams'][index].update(port=port))
                    self.assertFalse(result['ready'])
                    self.assertFalse(next(c for c in result['checks'] if c['name'] ==
                                          ('receive_profile', 'receiver_profile')[index])['ok'])

    def test_unsupported_receiver_mode_and_frequency_fail_without_io(self):
        for settings in ({'mode': 'DMR'}, {'frequency_hz': 1}, {'baud': 12345}, {'period_s': -1}):
            with self.subTest(settings=settings):
                self.assertFalse(self.preflight(lambda p: p['streams'][1]['settings'].update(settings))['ready'])

    def test_valid_serial_configuration_is_not_a_hardware_acceptance(self):
        for mode in ('FM', 'AM', ' fm '):
            with self.subTest(mode=mode):
                self.assertTrue(self.preflight(lambda p: p['streams'][1]['settings'].update(mode=mode))['ready'])
