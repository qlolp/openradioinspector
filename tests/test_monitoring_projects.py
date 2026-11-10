import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from radioinspector.monitoring.service import MonitoringService
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.web.server import create_server
from tests.test_device_workers import simulated_session_payload
from tests.monitoring_serial_fixture import monitoring_serial_session


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

    def serial_preflight(self, payload, *, windows=False):
        sessions = SessionService(); workers = DeviceWorkerManager(sessions)
        service = MonitoringService(sessions, workers)
        self.addCleanup(sessions.close); self.addCleanup(workers.close); self.addCleanup(service.close)
        session = sessions.create_session(payload)
        project = service.create(dict(name='Serial', site='Synthetic', session_id=session.id))
        service.save_preparation(project['id'], dict(antenna_notes='Synthetic declaration',
            receive_path_confirmed=True, location_confirmed=True))
        # Exercise COM validation on every CI host without changing the real
        # os module used by filesystem helpers or background threads.
        with patch('radioinspector.serial_instruments.PosixSerialTransport') as posix, \
                patch('radioinspector.serial_instruments.WindowsSerialTransport') as windows_transport, \
                patch('radioinspector.monitoring.service.os', SimpleNamespace(name='nt' if windows else os.name)):
            result = service.preflight(project['id'])
            posix.assert_not_called(); windows_transport.assert_not_called()
        self.assertEqual(workers.active_count, 0)
        self.assertEqual(sessions.list_sweeps(session.id), [])
        self.assertEqual(result['hardware_validation'], 'not_run')
        return result

    def test_serial_preflight_rejects_missing_or_simulated_ports_for_both_streams(self):
        for index in (0, 1):
            for port in (None, '', '   ', 'sim://invalid'):
                with self.subTest(index=index, port=port):
                    payload = monitoring_serial_session()
                    payload['streams'][index]['port'] = port
                    result = self.serial_preflight(payload)
                    self.assertFalse(result['ready'])
                    self.assertTrue(any(not check['ok'] and 'port' in check['name'] for check in result['checks']))

    def test_serial_preflight_rejects_invalid_baud_for_both_streams(self):
        for index in (0, 1):
            for baud in (4800, None, 'fast', [], True):
                with self.subTest(index=index, baud=baud):
                    payload = monitoring_serial_session()
                    payload['streams'][index]['settings']['baud'] = baud
                    result = self.serial_preflight(payload)
                    self.assertFalse(result['ready'])
                    self.assertTrue(any(not check['ok'] and 'baud' in check['name'] for check in result['checks']))

    def test_serial_preflight_requires_receiver_profile_for_digital_and_auto_modes(self):
        for profile, mode in ((None, 'DMR'), (None, 'AUTO'), ('unknown', 'FM'),
                              ('ardv1-monitor-v1', 'USB')):
            with self.subTest(profile=profile, mode=mode):
                payload = monitoring_serial_session()
                payload['streams'][1]['settings'].update(receiver_profile=profile, mode=mode)
                result = self.serial_preflight(payload)
                self.assertFalse(result['ready'])
                self.assertTrue(any(not check['ok'] and check['name'] == 'receiver_profile' for check in result['checks']))

    def test_serial_preflight_rejects_receiver_frequency_outside_adapter_contract(self):
        for frequency in (True, '145500000', 99_990, 1_300_000_010, 145_500_001):
            with self.subTest(frequency=frequency):
                payload = monitoring_serial_session()
                payload['streams'][1]['settings']['frequency_hz'] = frequency
                self.assertFalse(self.serial_preflight(payload)['ready'])

    def test_serial_preflight_preserves_legacy_and_optional_verified_profile_modes(self):
        for windows in (False, True):
            for profile, modes in ((None, (None, 'FM', ' am ')),
                                   ('ardv1-monitor-v1', ('FM', 'AM', 'AUTO', 'DSTAR', 'YSF',
                                    'ALINCO', 'NXDN', 'P25', 'DPMR', 'DMR'))):
                for mode in modes:
                    with self.subTest(windows=windows, profile=profile, mode=mode):
                        payload = monitoring_serial_session()
                        payload['streams'][0]['settings']['baud'] = '9600'
                        payload['streams'][1]['settings'].update(receiver_profile=profile, mode=mode)
                        self.assertTrue(self.serial_preflight(payload, windows=windows)['ready'])

    def test_serial_preflight_already_rejects_invalid_tinysa_receive_settings(self):
        for field, value in (('receive_profile', 'unknown'), ('start_hz', 1),
                             ('points', 291), ('rbw_hz', 20_000)):
            with self.subTest(field=field):
                payload = monitoring_serial_session()
                payload['streams'][0]['settings'][field] = value
                result = self.serial_preflight(payload)
                self.assertFalse(result['ready'])
                self.assertTrue(any(not check['ok'] and check['name'] == 'receive_profile' for check in result['checks']))

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
