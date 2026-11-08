"""Synthetic pySerial/COM contract tests; no device handles are opened."""
import sys
import os
import select
import http.client
import json
import threading
import types
import unittest
from collections import deque
from unittest.mock import Mock, patch

from radioinspector import serial_instruments as instruments
from radioinspector.device_workers import WorkerError
from radioinspector.session_service import SessionService
from tests.test_serial_instruments import serial_session, TinySATransport, ARDV1Transport
from tests.test_device_workers import wait_until
from tests.test_tinysa_setup_guard import (
    IDENTITY_COMMANDS, IDENTITY_REPLIES, SETUP_COMMANDS, SCAN_COMMAND, SCAN_REPLY,
)
from radioinspector.web.server import create_server

try:
    import serial as pyserial
except ImportError:
    pyserial = None


class FakeSerial:
    def __init__(self, **settings):
        self.settings = settings
        self.is_open = False
        self.closed = 0
        self.received = bytearray()
        self.written = bytearray()
        self.write_sizes = deque()
        self.responses = {}
        self.input_resets = 0
        self.output_resets = 0

    def open(self):
        self.is_open = True

    def close(self):
        self.closed += 1
        self.is_open = False

    def reset_input_buffer(self):
        self.input_resets += 1
        self.received.clear()

    def reset_output_buffer(self):
        self.output_resets += 1

    @property
    def in_waiting(self):
        return len(self.received)

    def read(self, count):
        chunk = bytes(self.received[:count])
        del self.received[:count]
        return chunk

    def write(self, data):
        count = min(self.write_sizes.popleft(), len(data)) if self.write_sizes else len(data)
        self.written.extend(data[:count])
        if bytes(data[:count]) in self.responses:
            self.received.extend(self.responses[bytes(data[:count])])
        return count


class ScheduledSerial(FakeSerial):
    """Virtual-time COM stream: blocking reads advance to bytes or timeout."""

    def __init__(self, scan_chunks):
        super().__init__()
        self.now = 0.0
        self.timeout = 0.0
        self.pending = deque()
        self.scan_chunks = scan_chunks
        self.commands = []

    def write(self, data):
        self.written.extend(data)
        command = bytes(data).decode("ascii").rstrip("\r")
        self.commands.append(command)
        if command == SCAN_COMMAND:
            self.pending.extend((self.now + delay, chunk) for delay, chunk in self.scan_chunks)
        else:
            reply = IDENTITY_REPLIES.get(command, command + "\r\nch> ")
            self.received.extend(reply.encode("ascii"))
        return len(data)

    def read(self, count):
        if not self.received:
            deadline = self.now + self.timeout
            if self.pending and self.pending[0][0] <= deadline:
                self.now, chunk = self.pending.popleft()
                self.received.extend(chunk)
            else:
                self.now = deadline
        return super().read(count)


class WindowsSerialTransportTests(unittest.TestCase):
    def setUp(self):
        self.port = FakeSerial()
        self.factory = Mock(return_value=self.port)
        self.serial = types.SimpleNamespace(Serial=self.factory)
        self.patch = patch.dict(sys.modules, serial=self.serial)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def transport(self, path='COM12', baud=9600):
        result = instruments.WindowsSerialTransport(path, baud)
        self.addCleanup(result.close)
        return result

    def test_open_configures_bounded_raw_io_and_resets_only_at_open(self):
        transport = self.transport()
        self.factory.assert_called_once_with(
            port=None, baudrate=9600, bytesize=8, parity='N', stopbits=1,
            timeout=0, write_timeout=1.0, xonxoff=False, rtscts=False, dsrdtr=False,
        )
        self.assertEqual(self.port.port, 'COM12')
        self.assertFalse(self.port.dtr)
        self.assertFalse(self.port.rts)
        transport.send(b'VER\r')
        self.assertEqual(bytes(self.port.written), b'VER\r')
        self.assertEqual((self.port.input_resets, self.port.output_resets), (1, 1))

    def test_invalid_ports_and_baud_fail_before_dependency_or_open(self):
        for path in ('', ' COM1', 'COM0', 'COM01', 'COM1\n', 'COM１', '/dev/ttyUSB0',
                     'socket://localhost:1234', None):
            with self.subTest(path=path), self.assertRaises(WorkerError):
                self.transport(path)
        for baud in (True, 9600.0, '9600', 12345):
            with self.subTest(baud=baud), self.assertRaises(WorkerError):
                self.transport(baud=baud)
        self.factory.assert_not_called()

    def test_extended_com_name_and_case_are_normalized(self):
        self.transport(r'\\.\com12')
        self.assertEqual(self.port.port, 'COM12')

    def test_missing_dependency_has_install_instruction(self):
        with patch.dict(sys.modules, serial=None), self.assertRaisesRegex(WorkerError, 'serial.*pip|pip.*serial'):
            self.transport()

    def test_partial_writes_and_zero_progress(self):
        transport = self.transport()
        self.port.write_sizes.extend([1, 2, 1])
        transport.send(b'LM\r')
        self.assertEqual(self.port.written, b'LM\r')
        self.port.write_sizes = deque([0])
        with self.assertRaises(WorkerError):
            transport.send(b'EX\r')

    def test_write_timeout_and_unplug_are_worker_errors(self):
        transport = self.transport()
        for error in (TimeoutError('write timeout'), OSError('device removed')):
            with patch.object(self.port, 'write', side_effect=error), self.assertRaises(WorkerError):
                transport.send(b'LM\r')
        with patch.object(self.port, 'read', side_effect=OSError('device removed')), self.assertRaises(WorkerError):
            transport.read_line(0.1, 'LM')

    def test_line_framing_keeps_buffered_reply_and_raw_crlf(self):
        transport = self.transport()
        self.port.received.extend(b'20VER123\r\n20LM0551\r\n')
        self.assertEqual(transport.read_line(0.1, 'VER'), '20VER123')
        self.assertEqual(transport.read_line(0.1, 'LM'), '20LM0551')
        self.assertEqual(self.port.input_resets, 1)

    def test_prompt_framing_and_empty_timeout(self):
        transport = self.transport()
        self.port.received.extend(b'output off\r\nch> ')
        self.assertEqual(transport.read_until_quiet(0.01, 0.05, 'setup'), 'output off\r\nch> ')
        with self.assertRaises(WorkerError):
            transport.read_line(0.001, 'LM')
        with self.assertRaises(WorkerError):
            transport.read_until_quiet(0.001, 0.002, 'scan')

    def delayed_adapter(self, scan_chunks):
        self.port = ScheduledSerial(scan_chunks)
        self.factory.return_value = self.port
        transport = self.transport()
        session = SessionService().create_session(serial_session())
        adapter = instruments.TinySASerialAdapter(session.streams[0], transport=transport)
        self.addCleanup(adapter.close)
        clock = types.SimpleNamespace(monotonic=lambda: self.port.now)
        return adapter, transport, clock

    def test_delayed_scan_echo_rows_and_fragmented_prompt_use_overall_deadline(self):
        echo = (SCAN_COMMAND + "\r\n").encode("ascii")
        rows = SCAN_REPLY.removesuffix("ch> ").encode("ascii")
        first, rest = rows.split(b"\r\n", 1)
        cases = (
            ((0.0, echo), (0.6, rows + b"ch> ")),
            ((0.6, echo), (1.2, rows + b"ch> ")),
            ((0.0, echo + first + b"\r\n"), (0.6, rest + b"ch> ")),
            ((0.0, echo + rows), (0.4, b"c"), (0.8, b"h"), (1.2, b"> ")),
        )
        for chunks in cases:
            with self.subTest(chunks=chunks):
                adapter, transport, clock = self.delayed_adapter(chunks)
                with patch.object(instruments, "time", clock):
                    sweep = adapter.poll()
                self.assertEqual(sweep.levels, (-90.0, -42.5, -80.0))
                self.assertEqual(self.port.commands, [*IDENTITY_COMMANDS, *SETUP_COMMANDS, SCAN_COMMAND])
                self.assertEqual(transport._stash, bytearray())
                self.assertFalse(adapter.details()["restart_required"])
                self.assertEqual(self.port.now, chunks[-1][0])
                self.assertEqual((self.port.input_resets, self.port.output_resets), (1, 1))

    def test_incomplete_scan_waits_to_deadline_then_taints_without_retry(self):
        chunks = ((0.0, (SCAN_COMMAND + "\r\n100000000 -90 0\r\n").encode("ascii")),
                  (20.0, b"ch> "))
        adapter, transport, clock = self.delayed_adapter(chunks)
        original_read = transport.read_until_quiet
        def bounded_read(quiet, maximum, label):
            return original_read(0.1, 0.5, label)
        with patch.object(instruments, "time", clock), patch.object(
                transport, "read_until_quiet", side_effect=bounded_read):
            with self.assertRaisesRegex(WorkerError, "перезапуск"):
                adapter.poll()
            self.assertAlmostEqual(self.port.now, 0.5)
            before = bytes(self.port.written)
            pending = list(self.port.pending)
            for _ in range(2):
                with self.assertRaisesRegex(WorkerError, "перезапуск"):
                    adapter.poll()
            self.assertEqual(bytes(self.port.written), before)
            self.assertEqual(list(self.port.pending), pending)
            self.assertEqual(pending, [(20.0, b"ch> ")])
        self.assertEqual(adapter.details()["generator_output"], "unknown")
        self.assertTrue(adapter.details()["restart_required"])

    def test_continuous_partial_reply_does_not_extend_scan_deadline(self):
        chunks = ((0.0, (SCAN_COMMAND + "\r\n").encode("ascii")),
                  (0.2, b"100000000 -90 0\r\n"),
                  (0.4, b"101000000 -42.5 0\r\n"),
                  (0.6, b"102000000 -80 0\r\nch> "))
        adapter, transport, clock = self.delayed_adapter(chunks)
        original_read = transport.read_until_quiet
        with patch.object(instruments, "time", clock), patch.object(
                transport, "read_until_quiet",
                side_effect=lambda quiet, maximum, label: original_read(0.25, 0.5, label)):
            with self.assertRaisesRegex(WorkerError, "таймаут.*перезапуск"):
                adapter.poll()
            self.assertAlmostEqual(self.port.now, 0.5)
            self.assertTrue(transport._stash.endswith(b"101000000 -42.5 0\r\n"))
            pending = list(self.port.pending)
            with self.assertRaisesRegex(WorkerError, "перезапуск"):
                adapter.poll()
            self.assertEqual(list(self.port.pending), pending)
            self.assertEqual(pending, [chunks[-1]])
        self.assertEqual(self.port.commands.count(SCAN_COMMAND), 1)
        self.assertTrue(adapter.details()["restart_required"])

    def test_silent_scan_uses_deadline_and_taints_connection(self):
        adapter, transport, clock = self.delayed_adapter(())
        original_read = transport.read_until_quiet
        with patch.object(instruments, "time", clock), patch.object(
                transport, "read_until_quiet",
                side_effect=lambda quiet, maximum, label: original_read(0.1, 0.5, label)):
            with self.assertRaisesRegex(WorkerError, "Пустой ответ.*перезапуск"):
                adapter.poll()
            self.assertAlmostEqual(self.port.now, 0.5)
        self.assertEqual(transport._stash, bytearray())
        self.assertEqual(adapter.details()["generator_output"], "unknown")
        self.assertTrue(adapter.details()["restart_required"])

    def test_close_is_idempotent_and_closed_transport_rejects_io(self):
        transport = self.transport()
        transport.close()
        transport.close()
        self.assertEqual(self.port.closed, 1)
        with self.assertRaises(WorkerError):
            transport.send(b'LM\r')
        with self.assertRaises(WorkerError):
            transport.read_line(0.01, 'LM')

    def test_setup_failure_closes_partially_opened_handle(self):
        for step in ('open', 'reset_input_buffer', 'reset_output_buffer'):
            self.port.closed = 0
            with patch.object(self.port, step, side_effect=OSError('setup failed')):
                with self.assertRaisesRegex(WorkerError, 'setup failed'):
                    self.transport()
            self.assertEqual(self.port.closed, 1)

    def test_platform_selection_is_core_side_and_simulator_never_opens_com(self):
        with patch.object(instruments, 'os', types.SimpleNamespace(name='nt')):
            transport = instruments.create_serial_transport('COM12', 9600)
            self.addCleanup(transport.close)
            self.assertIsInstance(transport, instruments.WindowsSerialTransport)
        with patch.object(instruments, 'os', types.SimpleNamespace(name='posix')):
            with patch.object(instruments, 'PosixSerialTransport') as posix:
                instruments.create_serial_transport('/dev/ttyUSB0', 9600)
                posix.assert_called_once_with('/dev/ttyUSB0', 9600)

    def test_both_adapters_select_windows_transport_without_client_changes(self):
        service = SessionService()
        self.addCleanup(service.close)
        payload = serial_session()
        payload['streams'][0]['port'] = 'COM12'
        payload['streams'][1]['port'] = 'COM13'
        session = service.create_session(payload)
        ports = [FakeSerial(), FakeSerial()]
        self.factory.side_effect = ports
        with patch.object(instruments, 'os', types.SimpleNamespace(name='nt')):
            adapters = [instruments.create_device_adapter(stream) for stream in session.streams]
        for adapter in adapters:
            self.addCleanup(adapter.close)
            self.assertIsInstance(adapter.transport, instruments.WindowsSerialTransport)
        self.assertIsNot(adapters[0].transport, adapters[1].transport)
        self.assertEqual([port.port for port in ports], ['COM12', 'COM13'])

    def test_real_adapters_parse_com_bytes_and_preserve_raw_units(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(serial_session())
        for index, scripted in enumerate((TinySATransport.valid(), ARDV1Transport.valid())):
            with self.subTest(device=index):
                self.port = FakeSerial()
                self.factory.return_value = self.port
                self.port.responses = {
                    command.encode() + b'\r': (replies[0] + '\r\n').encode()
                    for command, replies in scripted.responses.items()
                }
                transport = self.transport('COM12' if index == 0 else 'COM13')
                adapter = instruments.create_device_adapter(session.streams[index], transport=transport)
                self.addCleanup(adapter.close)
                measurement = adapter.poll()
                self.assertEqual(measurement.level_unit, 'dBm' if index == 0 else 's_meter_count')
                provenance = measurement.settings if index == 0 else measurement.metadata
                self.assertEqual(provenance['hardware_validation'], 'not_run')
                if index == 0:
                    self.assertEqual(measurement.levels, (-90.0, -42.5, -80.0))
                    self.assertIn(b'output off\r', self.port.written)
                else:
                    self.assertEqual(measurement.frequency_hz, 145_500_000)

    def test_missing_setup_prompt_poisoned_com_is_not_reused(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(serial_session())
        self.port.responses = {
            command.encode() + b'\r': (replies[0] + '\r\n').encode()
            for command, replies in TinySATransport.valid().responses.items()
        }
        self.port.responses[b'output off\r'] = b'output off\r\n'
        adapter = instruments.create_device_adapter(session.streams[0], transport=self.transport())
        self.addCleanup(adapter.close)
        with self.assertRaisesRegex(WorkerError, 'перезапуск'):
            adapter.poll()
        sent = bytes(self.port.written)
        self.port.received.extend(b'ch> ')
        with self.assertRaisesRegex(WorkerError, 'перезапуск'):
            adapter.poll()
        self.assertEqual(bytes(self.port.written), sent)
        self.assertNotIn(b'scan ', sent)

    def test_pending_reply_is_not_relabelled_as_next_ack(self):
        for buffered in (False, True):
            with self.subTest(buffered=buffered):
                self.port = FakeSerial()
                self.factory.return_value = self.port
                transport = self.transport()
                self.port.received.extend(b'20\r\n')
                if buffered:
                    transport._read_once(0, 'unsolicited')
                with self.assertRaisesRegex(WorkerError, 'Непрочитанный'):
                    transport.send(b'VFA\r')
                self.assertEqual(self.port.written, b'')
                self.assertEqual(self.port.input_resets, 1)

    def test_duplicate_ack_taints_adapter_before_next_mutation(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(serial_session())
        self.port.responses = {
            command.encode() + b'\r': (replies[0] + '\r\n').encode()
            for command, replies in ARDV1Transport.valid().responses.items()
        }
        self.port.responses[b'VFA\r'] = b'20\r\n20\r\n'
        adapter = instruments.create_device_adapter(session.streams[1], transport=self.transport())
        self.addCleanup(adapter.close)
        with self.assertRaisesRegex(WorkerError, 'Непрочитанный.*перезапуск'):
            adapter.poll()
        sent = bytes(self.port.written)
        self.assertNotIn(b'MD0F0\r', sent)
        self.assertIsNone(adapter.details()['frequency_hz'])
        with self.assertRaisesRegex(WorkerError, 'перезапуск'):
            adapter.poll()
        self.assertEqual(bytes(self.port.written), sent)

    def test_close_during_read_cannot_return_success_or_leave_stale_bytes(self):
        transport = self.transport()
        def read_then_close(count):
            transport.close()
            return b'2'
        with patch.object(self.port, 'read', side_effect=read_then_close):
            with self.assertRaisesRegex(WorkerError, 'закрыт'):
                transport._read_once(0.01, 'LM')
        self.assertEqual(transport._stash, b'')

    def test_close_during_write_cannot_report_success(self):
        transport = self.transport()
        def write_then_close(data):
            transport.close()
            return len(data)
        with patch.object(self.port, 'write', side_effect=write_then_close):
            with self.assertRaisesRegex(WorkerError, 'закрыт'):
                transport.send(b'LM\r')

    def test_setup_failure_preserves_original_error_when_close_also_fails(self):
        with patch.object(self.port, 'open', side_effect=OSError('original setup failure')):
            with patch.object(self.port, 'close', side_effect=OSError('cleanup failure')) as close:
                with self.assertRaisesRegex(WorkerError, 'original setup failure'):
                    self.transport()
                close.assert_called_once()

    def test_partial_write_shares_one_deadline(self):
        transport = self.transport()
        self.port.write_sizes = deque([1, 1, 1])
        with patch.object(instruments.time, 'monotonic', side_effect=[10.0, 10.1, 11.1]):
            with self.assertRaisesRegex(WorkerError, 'Таймаут'):
                transport.send(b'LM\r')
        self.assertEqual(self.port.written, b'L')
        self.assertAlmostEqual(self.port.write_timeout, 0.9)

    def test_device_error_bytes_cannot_become_setup_success(self):
        service = SessionService()
        self.addCleanup(service.close)
        session = service.create_session(serial_session())
        for index, command, reply, forbidden in (
            (0, b'output off\r', b'ERROR\r\nch> ', b'mode input\r'),
            (1, b'VFA\r', b'50\r\n', b'MD0F0\r'),
            (1, b'VFA\r', b'ERROR\r\n', b'MD0F0\r'),
        ):
            with self.subTest(device=index, reply=reply):
                self.port = FakeSerial()
                self.factory.return_value = self.port
                scripted = (TinySATransport.valid(), ARDV1Transport.valid())[index]
                self.port.responses = {
                    name.encode() + b'\r': (replies[0] + '\r\n').encode()
                    for name, replies in scripted.responses.items()
                }
                self.port.responses[command] = reply
                adapter = instruments.create_device_adapter(session.streams[index], transport=self.transport())
                self.addCleanup(adapter.close)
                with self.assertRaisesRegex(WorkerError, 'перезапуск'):
                    adapter.poll()
                self.assertNotIn(forbidden, self.port.written)
                self.assertTrue(adapter.details()['restart_required'])

    def test_http_two_com_workers_archive_raw_data_and_isolate_unplug(self):
        service = SessionService()
        server = create_server(host='127.0.0.1', port=0, sessions=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(3)
            server.server_close()
            for name in ('control_scheduler', 'automatic_completion', 'automatic_detection',
                         'automatic_handoff', 'worker_manager'):
                getattr(server, name).close()
            service.close()
        self.addCleanup(cleanup)

        def post(path, payload):
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request('POST', '/api/v1' + path, json.dumps(payload),
                                   {'Content-Type': 'application/json'})
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertIn(response.status, (200, 201), result)
                return result['data']
            finally:
                connection.close()

        ports = [FakeSerial(), FakeSerial()]
        for port, scripted in zip(ports, (TinySATransport.valid(), ARDV1Transport.valid())):
            port.responses = {command.encode() + b'\r': (replies[0] + '\r\n').encode()
                              for command, replies in scripted.responses.items()}
        self.factory.side_effect = ports
        payload = serial_session(max_errors=1)
        payload['streams'][0]['port'] = 'COM12'
        payload['streams'][1]['port'] = 'COM13'
        with patch.object(instruments, 'os', types.SimpleNamespace(name='nt')):
            session = post('/sessions', payload)
            self.assertEqual(session['state'], 'draft')
            self.factory.assert_not_called()
            prefix = '/sessions/' + session['id']
            post(prefix + '/start', {})
            post(prefix + '/workers/start', {})
            wait_until(lambda: len(service.list_sweeps(session['id'])) >= 2 and
                       len(service.list_observations(session['id'])) >= 2, 'Both COM workers must archive')
            with patch.object(ports[0], 'write', side_effect=OSError('USB disconnected')):
                wait_until(lambda: server.worker_manager.list_workers(session['id'])[0]['state'] == 'error',
                           'tinySA must report unplug')
                before = len(service.list_observations(session['id']))
                wait_until(lambda: len(service.list_observations(session['id'])) > before,
                           'AR-DV1 must continue after tinySA unplug')
            self.assertEqual(service.list_sweeps(session['id'])[0].level_unit, 'dBm')
            self.assertEqual(service.list_observations(session['id'])[0].level_unit, 's_meter_count')
            post(prefix + '/stop', {})
        self.assertFalse(any(port.is_open for port in ports))


@unittest.skipUnless(os.name == 'posix', 'Own PTY required')
class PendingInputPosixParityTests(unittest.TestCase):
    def test_pending_reply_fails_closed_in_both_buffers(self):
        import pty
        for buffered in (False, True):
            with self.subTest(buffered=buffered):
                master, slave = pty.openpty()
                try:
                    transport = instruments.PosixSerialTransport(os.ttyname(slave))
                    try:
                        os.write(master, b'20\r\n')
                        self.assertTrue(select.select([transport._fd], [], [], 0.5)[0])
                        if buffered:
                            transport._read_once(0, 'unsolicited')
                        with self.assertRaisesRegex(WorkerError, 'Непрочитанный'):
                            transport.send(b'VFA\r')
                        self.assertEqual(select.select([master], [], [], 0.01)[0], [])
                        self.assertEqual(transport._stash, b'20\r\n')
                    finally:
                        transport.close()
                finally:
                    os.close(master)
                    os.close(slave)


@unittest.skipUnless(pyserial is not None, 'Install the serial extra for a real pySerial loopback')
class PySerialLoopbackTests(unittest.TestCase):
    def test_actual_pyserial_byte_io_timeouts_and_close_without_device(self):
        port = pyserial.serial_for_url('loop://', do_not_open=True)
        original_open = port.open

        def open_loop():
            # Only the test replaces the native COM backend with pySerial's own
            # in-memory loop. Production rejects URL handlers before importing it.
            port.port = 'loop://'
            original_open()

        with patch.object(port, 'open', side_effect=open_loop), patch.object(pyserial, 'Serial', return_value=port):
            transport = instruments.WindowsSerialTransport('COM12')
        try:
            transport.send(b'20VER123\r\n20LM0551\r\n')
            self.assertEqual(transport.read_line(0.1, 'VER'), '20VER123')
            self.assertEqual(transport.read_line(0.1, 'LM'), '20LM0551')
            with self.assertRaises(WorkerError):
                transport.read_line(0.01, 'empty')
            transport.send(b'output off\r\nch> ')
            self.assertEqual(transport.read_until_quiet(0.01, 0.1, 'setup'), 'output off\r\nch> ')
        finally:
            transport.close()
        self.assertFalse(port.is_open)


if __name__ == '__main__':
    unittest.main()
