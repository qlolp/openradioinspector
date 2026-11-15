"""Deterministic cancellation and new-connection recovery, without hardware."""
import threading
import unittest
from unittest.mock import patch

from radioinspector.device_workers import DeviceWorker, TinySASimulator, WorkerCancelled, WorkerError
from radioinspector.serial_instruments import _SerialFraming, TinySASerialAdapter, _TINYSA_PENDING_UNTIL
from radioinspector.session_service import SessionService
from tests.test_device_workers import simulated_session_payload, wait_until
from tests.test_serial_instruments import serial_session
from tests.test_tinysa_setup_guard import FIFOTinySATransport


class StopTests(unittest.TestCase):
    def worker(self, *, fail=False, ignore_cancel=False):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(simulated_session_payload())
        service.start_session(session.id)
        entered, released = threading.Event(), threading.Event()
        class Blocking(TinySASimulator):
            cancelled = False
            closed = False
            def poll(self):
                entered.set()
                if not released.wait(3):
                    raise AssertionError('test did not release sweep')
                if fail:
                    raise WorkerError('real device fault')
                if self.cancelled and not ignore_cancel:
                    raise WorkerCancelled('explicit cancellation')
                return super().poll()
            def request_stop(self):
                self.cancelled = True
                released.set()
            def close(self):
                self.closed = True
        adapter = Blocking(session.streams[0])
        worker = DeviceWorker(service, adapter)
        self.addCleanup(lambda: (released.set(), worker.stop()))
        worker.start()
        self.assertTrue(entered.wait(1))
        return worker, released, service

    def test_graceful_finishes_and_archives_current_sweep(self):
        worker, released, service = self.worker()
        worker.stop(mode='graceful')
        self.assertTrue(worker.status()['stopping'])
        self.assertEqual(worker.status()['stop_mode'], 'graceful')
        self.assertFalse(worker.adapter.cancelled)
        released.set()
        wait_until(lambda: not worker.alive, 'graceful completion')
        self.assertEqual(worker.status()['measurements'], 1)
        self.assertTrue(worker.adapter.closed)
        self.assertEqual(worker.status()['state'], 'disconnected')

    def test_force_escalates_graceful_without_counting_cancellation_as_fault(self):
        worker, _, service = self.worker()
        worker.stop(mode='graceful')
        worker.stop(mode='force')
        status = worker.status()
        self.assertFalse(status['alive'])
        self.assertEqual(status['consecutive_errors'], 0)
        self.assertIsNone(status['last_error'])
        self.assertEqual(status['measurements'], 0)
        self.assertTrue(worker.adapter.closed)

    def test_force_discards_poll_that_returns_despite_cancellation(self):
        worker, _, service = self.worker(ignore_cancel=True)
        worker.stop(mode='force')
        self.assertEqual(worker.status()['measurements'], 0)
        self.assertEqual(service.list_sweeps(worker.session_id), [])
        self.assertIsNone(worker.status()['last_error'])

    def test_real_error_during_graceful_stop_is_not_hidden(self):
        worker, released, _ = self.worker(fail=True)
        worker.stop(mode='graceful')
        released.set()
        wait_until(lambda: not worker.alive, 'failed completion')
        self.assertEqual(worker.status()['consecutive_errors'], 1)
        self.assertEqual(worker.status()['last_error'], 'real device fault')


class RecoveryTests(unittest.TestCase):
    def framing(self, schedule):
        class Framing(_SerialFraming):
            def __init__(self):
                self.path = 'synthetic'
                self.now = 0.0
                self._cancelled = threading.Event()
                self._stash = bytearray()
            def _read_once(self, timeout, label):
                self.now += timeout
                chunk = schedule(self.now)
                self._stash.extend(chunk)
                return chunk
        return Framing()

    def test_silent_old_scan_then_fragmented_prompt_is_drained_before_ready(self):
        chunks = iter([b'old scan\r\n', b'ch', b'> '])
        framing = self.framing(lambda now: next(chunks, b'') if now >= 2 else b'')
        with patch('radioinspector.serial_instruments.time.monotonic', side_effect=lambda: framing.now):
            framing.synchronize_tinysa(90)
        self.assertGreater(framing.now, 2.4)
        self.assertLess(framing.now, 3)
        self.assertEqual(framing._stash, b'')

    def test_lost_prompt_fails_closed_after_pending_budget(self):
        framing = self.framing(lambda now: b'')
        with patch('radioinspector.serial_instruments.time.monotonic', side_effect=lambda: framing.now):
            with self.assertRaisesRegex(WorkerError, "synchronization timed out"):
                framing.synchronize_tinysa(90)
        self.assertGreaterEqual(framing.now, 90)

    def test_continuing_bytes_fail_closed_with_bounded_time(self):
        framing = self.framing(lambda now: b'x')
        with patch('radioinspector.serial_instruments.time.monotonic', side_effect=lambda: framing.now):
            with self.assertRaisesRegex(WorkerError, 'synchronization timed out'):
                framing.synchronize_tinysa(1)
        self.assertLess(framing.now, 4.2)

    def test_sync_failure_never_sends_identity_or_retries_connection(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(serial_session())
        class Transport(FIFOTinySATransport):
            def synchronize_tinysa(self, pending_until):
                raise WorkerError('synchronization timed out')
        transport = Transport()
        adapter = TinySASerialAdapter(session.streams[0], transport=transport)
        self.addCleanup(adapter.close)
        for _ in range(2):
            with self.assertRaises(WorkerError):
                adapter.poll()
        self.assertEqual(transport.sent, [])

    def test_sync_runs_once_before_info_on_new_connection(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(serial_session())
        class Transport(FIFOTinySATransport):
            calls = 0
            def synchronize_tinysa(self, pending_until):
                self.calls += 1
                if self.sent:
                    raise AssertionError('synchronization after command')
        transport = Transport()
        adapter = TinySASerialAdapter(session.streams[0], transport=transport)
        self.addCleanup(adapter.close)
        adapter.poll()
        adapter.poll()
        self.assertEqual(transport.calls, 1)
        self.assertEqual(transport.sent[0], 'info')

    def test_interrupted_port_deadline_is_passed_only_to_new_connection(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(serial_session())
        class Transport(FIFOTinySATransport):
            path = '/synthetic/tinysa-recovery'
            recovery_deadlines = None
            def synchronize_tinysa(self, pending_until):
                self.recovery_deadlines = pending_until
        with patch.dict(_TINYSA_PENDING_UNTIL, {}, clear=True):
            first = Transport()
            first.replies['scan 100M 101M 3 3'] = (WorkerCancelled('cancelled'),)
            adapter = TinySASerialAdapter(session.streams[0], transport=first)
            with self.assertRaises(WorkerCancelled):
                adapter.poll()
            adapter.close()
            self.assertEqual(first.recovery_deadlines, 0)
            second = Transport()
            replacement = TinySASerialAdapter(session.streams[0], transport=second)
            self.addCleanup(replacement.close)
            replacement.poll()
            self.assertGreater(second.recovery_deadlines, 0)
            self.assertEqual(second.sent[0], 'info')
            self.assertEqual(_TINYSA_PENDING_UNTIL, {})
