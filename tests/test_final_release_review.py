"""Reproductions from the fresh whole-branch release review."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from radioinspector.launcher import LocalCore
from radioinspector.device_workers import DeviceWorkerManager, TinySASimulator, WorkerError
from radioinspector.session_service import SessionService
from tests.test_device_workers import simulated_session_payload
from tests.test_investigations import candidate_payload


class ReviewRegressions(unittest.TestCase):
    def test_v1_rejects_browser_mutations_before_changing_state(self):
        with tempfile.TemporaryDirectory() as directory, LocalCore(Path(directory)) as core:
            for path, payload in [('/sessions', simulated_session_payload()),
                                  ('/sessions/untrusted/start', {})]:
                for content_type, status in [('text/plain', 415), ('application/json',403)]:
                    with self.subTest(path=path, content_type=content_type):
                        request = Request(core.url+'/api/v1'+path, data=json.dumps(payload).encode(),
                            headers={'Content-Type':content_type,'Origin':'https://untrusted.example'})
                        with self.assertRaises(HTTPError) as error:
                            urlopen(request)
                        self.assertEqual(error.exception.code,status)
                        self.assertEqual(json.load(error.exception)['error']['code'],'invalid_request')
            self.assertEqual(core.server.session_service.list_sessions(), [])

    def test_shutdown_retains_lease_and_archive_until_startup_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            core=LocalCore(Path(directory)); core.start()
            manager=core.server.worker_manager; service=core.server.session_service
            session=service.create_session(simulated_session_payload());service.start_session(session.id)
            entered=threading.Event();release=threading.Event();errors=[]
            def factory(stream, **kwargs):
                adapter=TinySASimulator(stream)
                entered.set();release.wait(5)
                return adapter
            def start():
                try:manager.start_stream(session.id,session.streams[0].id)
                except WorkerError as error:errors.append(error)
            thread=threading.Thread(target=start)
            try:
                with patch('radioinspector.serial_instruments.create_device_adapter',side_effect=factory):
                    thread.start();self.assertTrue(entered.wait(2))
                    with patch.object(manager,'_stop_worker',side_effect=WorkerError('startup still owns I/O')):
                        with self.assertRaisesRegex(RuntimeError,'I/O'):
                            core.close()
                    self.assertIsNotNone(core._lease)
                    with self.assertRaises(RuntimeError):LocalCore(Path(directory)).start()
                    self.assertEqual(service.get_session(session.id).id,session.id)
            finally:
                release.set();thread.join(5);core.close()
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)

    def test_invalid_first_adapter_settings_do_not_block_healthy_receiver(self):
        service=SessionService();manager=DeviceWorkerManager(service)
        try:
            session=service.create_session(simulated_session_payload(tinysa_settings={'points':0}))
            service.start_session(session.id)
            statuses=manager.start_session(session.id)
            self.assertEqual(statuses[0]['state'],'error')
            self.assertTrue(statuses[1]['alive'])
        finally:manager.close();service.close()

    def test_literal_investigation_id_next_has_unambiguous_get_route(self):
        with tempfile.TemporaryDirectory() as directory, LocalCore(Path(directory)) as core:
            service=core.server.session_service
            session=service.create_session(simulated_session_payload());service.start_session(session.id)
            candidate=service.create_candidate(session.id,candidate_payload(session.streams[0].id))
            item=service.enqueue_investigation(session.id,candidate.id,{'id':'next','target_stream_id':session.streams[1].id})
            with urlopen(core.url+f'/api/v1/sessions/{session.id}/investigations/by-id/next') as response:
                self.assertEqual(json.load(response)['data']['id'],item.id)
