"""POSIX transport checks on local pseudo terminals, never physical devices."""

from __future__ import annotations

import os
import select
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

if os.name == "posix":
    import pty
    import termios

from radioinspector.device_workers import WorkerError
from radioinspector.serial_instruments import ARDV1SerialAdapter, PosixSerialTransport
from radioinspector.session_service import SessionService
from tests.test_serial_instruments import serial_session


@unittest.skipUnless(os.name == "posix", "POSIX pseudo terminals required")
class PosixSerialTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.path = os.ttyname(self.slave)
        self.original = termios.tcgetattr(self.slave)

    def assert_settings_restored(self) -> None:
        restored = termios.tcgetattr(self.slave)
        original = list(self.original)
        # Darwin sets PENDIN when returning to canonical mode. It is transient
        # kernel state, not a persistent line setting selected by the caller.
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        original[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(restored, original)

    def open_transport(self, baud: int = 9600) -> PosixSerialTransport:
        transport = PosixSerialTransport(self.path, baud)
        self.addCleanup(transport.close)
        return transport

    def test_raw_8n1_and_supported_baud_rates_restore_on_close(self) -> None:
        for baud in (9600, 19200, 38400, 57600, 115200):
            with self.subTest(baud=baud):
                transport = self.open_transport(baud)
                attrs = termios.tcgetattr(self.slave)
                self.assertEqual(attrs[4:6], [getattr(termios, f"B{baud}")] * 2)
                self.assertFalse(attrs[0] & (termios.ICRNL | termios.IXON | termios.IXOFF))
                self.assertFalse(attrs[1] & termios.OPOST)
                self.assertEqual(attrs[2] & termios.CSIZE, termios.CS8)
                self.assertFalse(attrs[2] & (termios.CSTOPB | termios.PARENB))
                self.assertTrue(attrs[2] & termios.CLOCAL)
                self.assertTrue(attrs[2] & termios.CREAD)
                self.assertFalse(attrs[3] & (termios.ICANON | termios.ECHO | termios.ISIG))
                self.assertEqual(attrs[6][termios.VMIN], 1)
                self.assertEqual(attrs[6][termios.VTIME], 0)
                fd = transport._fd
                transport.close()
                transport.close()
                self.assert_settings_restored()
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_raw_bytes_and_crlf_are_not_transformed_or_echoed(self) -> None:
        transport = self.open_transport()
        payload = b"\x03\x11\x13\xff\r\n"
        os.write(self.master, payload)
        self.assertEqual(transport._read_once(0.5, "synthetic bytes"), payload)
        self.assertEqual(bytes(transport._stash), payload)
        self.assertEqual(select.select([self.master], [], [], 0.01)[0], [])
        transport._stash.clear()
        os.write(self.master, b"LM0551\r\nLM1043\n")
        self.assertEqual(transport.read_line(0.5, "first"), "LM0551")
        self.assertEqual(transport.read_line(0.5, "second"), "LM1043")
        transport.send(payload)
        self.assertTrue(select.select([self.master], [], [], 0.5)[0])
        self.assertEqual(os.read(self.master, 4096), payload)

    def test_read_timeout_and_prompt(self) -> None:
        transport = self.open_transport()
        with self.assertRaisesRegex(WorkerError, "Таймаут"):
            transport.read_line(0.01, "empty PTY")
        os.write(self.master, b"synthetic reply\r\nch> ")
        self.assertEqual(
            transport.read_until_quiet(0.01, 0.5, "prompt"),
            "synthetic reply\r\nch> ",
        )

    def test_ardv1_synthetic_receiver_end_to_end_over_pty(self) -> None:
        payload = serial_session()
        payload["streams"][1]["port"] = self.path
        session = SessionService().create_session(payload)
        adapter = ARDV1SerialAdapter(session.streams[1])
        replies = {
            "RE1": "20", "VR": "20VR1805A", "RN": "20RNsynthetic",
            "VFA": "20", "MD0F0": "20", "RF0145.50000": "20",
            "LM": "20LM0551", "EX": "20",
        }
        commands = []
        errors = []
        stopped = threading.Event()
        def serve() -> None:
            pending = bytearray()
            deadline = time.monotonic() + 3.0
            try:
                while time.monotonic() < deadline and not stopped.is_set():
                    if not select.select([self.master], [], [], 0.05)[0]:
                        continue
                    pending.extend(os.read(self.master, 4096))
                    while b"\r" in pending:
                        raw, _, rest = pending.partition(b"\r")
                        pending[:] = rest
                        command = raw.decode("ascii")
                        commands.append(command)
                        os.write(self.master, (replies[command] + "\r\n").encode("ascii"))
                        if command == "EX":
                            return
            except Exception as exc:
                errors.append(exc)
        server = threading.Thread(target=serve, daemon=True)
        server.start()
        try:
            observation = adapter.poll()
            self.assertEqual(observation.frequency_hz, 145_500_000)
            self.assertEqual(observation.level, 55)
            self.assertEqual(observation.level_unit, "s_meter_count")
            self.assertEqual(observation.metadata["frequency_source"], "command_acknowledged")
            self.assertEqual(observation.metadata["hardware_validation"], "not_run")
        finally:
            try:
                adapter.close()
            finally:
                stopped.set()
                server.join(1.0)
        self.assertFalse(server.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(commands, list(replies))
        self.assert_settings_restored()

    def test_ardv1_late_ack_and_level_remain_unread_after_real_pty_timeout(self) -> None:
        session = SessionService().create_session(serial_session())
        for fault_command, delayed_reply in (("RF0433.07500", b"20\r\n"), ("LM", b"20LM0991\r\n")):
            with self.subTest(command=fault_command):
                transport = self.open_transport(115200)
                adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
                commands = []
                errors = []
                armed = threading.Event()
                release_late = threading.Event()
                late_sent = threading.Event()
                stopped = threading.Event()
                def serve() -> None:
                    pending = bytearray()
                    try:
                        while not stopped.is_set():
                            if not select.select([self.master], [], [], 0.02)[0]:
                                continue
                            pending.extend(os.read(self.master, 4096))
                            while b"\r" in pending:
                                raw, _, rest = pending.partition(b"\r")
                                pending[:] = rest
                                command = raw.decode("ascii")
                                commands.append(command)
                                if command == fault_command and armed.is_set():
                                    if not release_late.wait(2.0):
                                        raise TimeoutError("test did not release late reply")
                                    os.write(self.master, delayed_reply)
                                    late_sent.set()
                                else:
                                    reply = {
                                        "VR": b"20VR1805A\r\n", "RN": b"20RNsynthetic\r\n",
                                        "LM": b"20LM0551\r\n",
                                    }.get(command, b"20\r\n")
                                    os.write(self.master, reply)
                    except Exception as exc:
                        errors.append(exc)
                server = threading.Thread(target=serve, daemon=True)
                server.start()
                original_read_line = transport.read_line
                def bounded_read_line(timeout: float, label: str) -> str:
                    if armed.is_set() and label == fault_command:
                        timeout = min(timeout, 0.03)
                    return original_read_line(timeout, label)
                try:
                    adapter.poll()
                    armed.set()
                    with patch.object(transport, "read_line", side_effect=bounded_read_line):
                        with self.assertRaisesRegex(WorkerError, "Таймаут.*перезапуск"):
                            if fault_command == "LM":
                                adapter.poll()
                            else:
                                adapter.execute("tune", {"frequency_hz": 433_075_000})
                    release_late.set()
                    self.assertTrue(late_sent.wait(1.0))
                    self.assertTrue(select.select([transport._fd], [], [], 0.5)[0])
                    before = list(commands)
                    with self.assertRaisesRegex(WorkerError, "перезапуск"):
                        adapter.execute("tune", {"frequency_hz": 434_000_000})
                    for _ in range(2):
                        with self.assertRaisesRegex(WorkerError, "перезапуск"):
                            adapter.poll()
                    self.assertEqual(commands, before)
                    self.assertIsNone(adapter.details()["frequency_hz"])
                    self.assertIsNone(adapter.details()["mode"])
                    self.assertTrue(adapter.details()["restart_required"])
                    # No later operation consumed/relabelled the late wire data.
                    self.assertEqual(os.read(transport._fd, 4096), delayed_reply)
                finally:
                    release_late.set()
                    stopped.set()
                    server.join(1.0)
                    adapter.close()
                self.assertFalse(server.is_alive())
                self.assertEqual(errors, [])
                self.assertNotIn("EX", commands)
                self.assert_settings_restored()

    def test_validation_happens_before_open(self) -> None:
        with patch("radioinspector.serial_instruments.os.open") as open_port:
            for path, baud in (("", 9600), (self.path, 123), (self.path, True)):
                with self.subTest(path=path, baud=baud), self.assertRaises(WorkerError):
                    PosixSerialTransport(path, baud)
            open_port.assert_not_called()

    def test_missing_optional_hardware_flow_flag(self) -> None:
        portable = SimpleNamespace(**{
            name: getattr(termios, name) for name in dir(termios) if name != "CRTSCTS"
        })
        with patch.dict("sys.modules", {"termios": portable}):
            transport = self.open_transport()
            transport.close()
        self.assert_settings_restored()

    def test_initialization_failure_restores_settings_and_closes_fd(self) -> None:
        real_open = os.open
        opened = []
        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        with patch("radioinspector.serial_instruments.os.open", side_effect=record_open):
            with patch.object(termios, "tcflush", side_effect=OSError("synthetic flush failure")):
                with self.assertRaisesRegex(WorkerError, "synthetic flush failure"):
                    PosixSerialTransport(self.path)
        self.assert_settings_restored()
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_getattr_failure_closes_fd_without_restoring_unknown_settings(self) -> None:
        real_close = os.close
        with patch.object(termios, "tcgetattr", side_effect=OSError("synthetic read failure")):
            with patch("radioinspector.serial_instruments.os.close", wraps=real_close) as close_fd:
                with self.assertRaisesRegex(WorkerError, "synthetic read failure"):
                    PosixSerialTransport(self.path)
                close_fd.assert_called_once()
                fd = close_fd.call_args.args[0]
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_restore_failure_still_closes_fd_and_close_is_idempotent(self) -> None:
        transport = self.open_transport()
        fd = transport._fd
        with patch.object(termios, "tcsetattr", side_effect=OSError("synthetic restore failure")):
            with self.assertRaisesRegex(WorkerError, "synthetic restore failure"):
                transport.close()
        with self.assertRaises(OSError):
            os.fstat(fd)
        transport.close()

    def test_write_timeout_does_not_attempt_nonready_write(self) -> None:
        transport = self.open_transport()
        with patch("radioinspector.serial_instruments.select.select", return_value=([], [], [])):
            with patch("radioinspector.serial_instruments.os.write") as write:
                with self.assertRaisesRegex(WorkerError, "Таймаут"):
                    transport.send(b"synthetic")
                write.assert_not_called()

    def test_write_handles_partial_and_temporarily_blocked_writes(self) -> None:
        transport = self.open_transport()
        with patch("radioinspector.serial_instruments.os.write", side_effect=[BlockingIOError(), 2, 2]) as write:
            transport.send(b"TEST")
        self.assertEqual([bytes(call.args[1]) for call in write.call_args_list], [b"TEST", b"TEST", b"ST"])

    def test_disconnect_is_not_accepted_as_partial_reply(self) -> None:
        transport = self.open_transport()
        def partial_then_disconnect(*args, **kwargs):
            if not transport._stash:
                transport._stash.extend(b"unfinished")
                return b"unfinished"
            raise WorkerError("synthetic disconnect")
        with patch.object(transport, "_read_once", side_effect=partial_then_disconnect):
            with self.assertRaisesRegex(WorkerError, "synthetic disconnect"):
                transport.read_until_quiet(0.01, 0.5, "partial")


if __name__ == "__main__":
    unittest.main()
