"""New-session serial ownership validation; synthetic payloads, no devices."""

import http.client
import json
import threading
import unittest
from unittest.mock import patch

from radioinspector.contracts import MonitoringSession
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_serial_instruments import serial_session


class SerialSessionValidationTests(unittest.TestCase):
    def service(self, sqlite=False):
        store = SQLiteSessionStore(':memory:') if sqlite else None
        service = SessionService(store=store)
        self.addCleanup(service.close)
        return service, store

    def payload(self, first='COM12', second='com12'):
        payload = serial_session()
        payload['streams'][0]['port'] = first
        payload['streams'][1]['port'] = second
        return payload

    def assert_empty(self, service, store):
        self.assertEqual(service.list_sessions(), [])
        self.assertEqual(service.events(), [])
        self.assertEqual(service.health()['last_event_id'], 0)
        if store:
            for table in ('sessions', 'streams', 'events'):
                self.assertEqual(store._connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_native_com_aliases_fail_before_memory_or_sqlite_persistence(self):
        for sqlite in (False, True):
            service, store = self.service(sqlite)
            for first, second in (('COM12', 'com12'), ('COM12', r'\\.\COM12'),
                                  (r'\\.\com12', 'COM12')):
                with self.subTest(sqlite=sqlite, first=first, second=second):
                    with patch('radioinspector.serial_instruments.WindowsSerialTransport') as windows:
                        with patch('radioinspector.serial_instruments.PosixSerialTransport') as posix:
                            with self.assertRaisesRegex(ValueError, 'порт'):
                                service.create_session(self.payload(first, second))
                            windows.assert_not_called()
                            posix.assert_not_called()
                    self.assert_empty(service, store)

    def test_distinct_ports_and_posix_case_remain_unchanged(self):
        for first, second in (('COM12', 'COM13'), ('/dev/ttyA', '/dev/ttya')):
            service, _ = self.service()
            session = service.create_session(self.payload(first, second))
            self.assertEqual([stream.port for stream in session.streams], [first, second])

    def test_simulator_and_nonserial_aliases_keep_existing_contract(self):
        for adapters in (('tinysa-sim', 'ardv1-sim'), (None, None), ('tinysa-serial', 'ardv1-sim')):
            service, _ = self.service()
            payload = self.payload()
            for stream, adapter in zip(payload['streams'], adapters):
                stream['settings']['adapter'] = adapter
            self.assertEqual(len(service.create_session(payload).streams), 2)

    def test_exact_posix_duplicates_still_fail(self):
        service, store = self.service(True)
        with self.assertRaisesRegex(ValueError, 'порт'):
            service.create_session(self.payload('/dev/ttyA', '/dev/ttyA'))
        self.assert_empty(service, store)

    def test_existing_archive_aliases_are_restored_without_migration(self):
        store = SQLiteSessionStore(':memory:')
        original = MonitoringSession.from_dict(self.payload())
        store.save_session(original)
        service = SessionService(store=store)
        self.addCleanup(service.close)
        self.assertEqual([stream.port for stream in service.get_session(original.id).streams], ['COM12', 'com12'])
        self.assertEqual(service.events(), [])

    def test_http_alias_returns_400_without_event_rows_or_port_opens(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite):
                service, store = self.service(sqlite)
                server = create_server(host='127.0.0.1', port=0, sessions=service)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with patch('radioinspector.serial_instruments.WindowsSerialTransport') as windows:
                        with patch('radioinspector.serial_instruments.PosixSerialTransport') as posix:
                            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                            try:
                                connection.request('POST', '/api/v1/sessions', json.dumps(self.payload()),
                                                   {'Content-Type': 'application/json'})
                                response = connection.getresponse()
                                result = json.loads(response.read())
                                self.assertEqual(response.status, 400, result)
                            finally:
                                connection.close()
                            windows.assert_not_called()
                            posix.assert_not_called()
                    self.assert_empty(service, store)
                finally:
                    server.shutdown()
                    thread.join(3)
                    server.server_close()
                    for name in ('control_scheduler', 'automatic_completion', 'automatic_detection',
                                 'automatic_handoff', 'worker_manager'):
                        getattr(server, name).close()
