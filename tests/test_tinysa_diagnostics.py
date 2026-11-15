"""Metadata flight recorder and explicit per-device recovery; no real USB."""
import json
import unittest
from unittest.mock import patch

from radioinspector.device_workers import DeviceWorkerManager, TinySASimulator, WorkerError
from radioinspector.serial_instruments import TinySASerialAdapter
from radioinspector.session_service import SessionService
from tests.test_device_workers import simulated_session_payload, wait_until
from tests.test_serial_instruments import serial_session
from tests.test_tinysa_setup_guard import FIFOTinySATransport


class DiagnosticTests(unittest.TestCase):
    def adapter(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(serial_session())
        transport = FIFOTinySATransport()
        adapter = TinySASerialAdapter(session.streams[0], transport=transport)
        self.addCleanup(adapter.close)
        return adapter, transport

    def test_failed_transaction_is_visible_without_raw_data_and_remains_after_close(self):
        adapter, transport = self.adapter()
        transport.replies['scan 100M 101M 3 3'] = (WorkerError('private wire response'),)
        with self.assertRaises(WorkerError):
            adapter.poll()
        self.assertIn('transactions', adapter.details())
        events = adapter.details()['transactions']
        self.assertEqual(events[-1]['command'], 'scan')
        self.assertEqual(events[-1]['outcome'], 'failed')
        self.assertFalse(events[-1]['terminal_received'])
        self.assertNotIn('private wire', json.dumps(events))
        self.assertNotIn('100M', json.dumps(events))
        adapter.close()
        self.assertEqual(adapter.details()['transactions'], events)

    def test_successful_recorder_is_bounded_and_detached(self):
        adapter, _ = self.adapter()
        for _ in range(80):
            adapter.poll()
        self.assertIn('transactions', adapter.details())
        events = adapter.details()['transactions']
        self.assertLessEqual(len(events), 64)
        self.assertEqual(events[-1]['outcome'], 'completed')
        self.assertTrue(events[-1]['terminal_received'])
        self.assertGreater(events[-1]['received_bytes'], 0)
        events[-1]['outcome'] = 'tampered'
        self.assertEqual(adapter.details()['transactions'][-1]['outcome'], 'completed')


class RecoveryManagerTests(unittest.TestCase):
    def test_explicit_recovery_replaces_only_target_and_waits_for_new_commit(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(simulated_session_payload())
        service.start_session(session.id)
        manager = DeviceWorkerManager(service)
        self.addCleanup(manager.close)
        manager.start_session(session.id)
        target, receiver = session.streams
        old = manager._workers[(session.id, target.id)]
        other = manager._workers[(session.id, receiver.id)]
        wait_until(lambda: old.status()['measurements'] > 0, 'first measurement')
        self.assertTrue(hasattr(manager, 'recover_stream'))
        # Recovery is limited to an already stopped/failed worker, not a restart
        # button which silently interrupts a running acquisition or manual lease.
        with self.assertRaises(WorkerError):
            manager.recover_stream(session.id, target.id)
        manager.stop_stream(session.id, target.id)
        result = manager.recover_stream(session.id, target.id)
        self.assertIsNot(manager._workers[(session.id, target.id)], old)
        self.assertIs(manager._workers[(session.id, receiver.id)], other)
        self.assertTrue(other.alive)
        self.assertTrue(result['recovery']['requires_new_measurement'])
        self.assertFalse(result['recovery']['watch_resumed'])

    def test_recovery_does_not_bypass_project_ownership(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(simulated_session_payload())
        service.start_session(session.id)
        manager = DeviceWorkerManager(service)
        self.addCleanup(manager.close)
        self.assertTrue(hasattr(manager, 'recover_stream'))
        def reject(*args):
            raise WorkerError('owned by monitoring project')
        manager.control_authorizer = reject
        with self.assertRaisesRegex(WorkerError, 'owned by monitoring project'):
            manager.recover_stream(session.id, session.streams[0].id)
        self.assertEqual(manager.active_count, 0)
