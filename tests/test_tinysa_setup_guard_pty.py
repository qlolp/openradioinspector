"""tinySA safety exchanges on this test's own POSIX PTYs, never hardware."""

from __future__ import annotations

import os
import select
import threading
import time
import unittest
from unittest.mock import patch

if os.name == "posix":
    import pty
    import termios

from radioinspector.device_workers import WorkerError
from radioinspector.serial_instruments import PosixSerialTransport, TinySASerialAdapter
from radioinspector.session_service import SessionService
from tests.test_serial_instruments import serial_session
from tests.test_tinysa_setup_guard import (
    IDENTITY_COMMANDS, IDENTITY_REPLIES, SCAN_COMMAND, SCAN_REPLY, SETUP_COMMANDS,
)


@unittest.skipUnless(os.name == "posix", "POSIX pseudo terminals required")
class TinySASetupPTYTests(unittest.TestCase):
    def setUp(self) -> None:
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.original = termios.tcgetattr(self.slave)
        self.transport = PosixSerialTransport(os.ttyname(self.slave), 9600)
        self.addCleanup(self.transport.close)
        session = SessionService().create_session(serial_session())
        self.adapter = TinySASerialAdapter(session.streams[0], transport=self.transport)
        self.commands: list[str] = []
        self.errors: list[Exception] = []
        self.stopped = threading.Event()
        self.release_late = threading.Event()
        self.late_sent = threading.Event()
        self.server: threading.Thread | None = None
        self.addCleanup(self.finish)

    def finish(self) -> None:
        self.stopped.set()
        self.release_late.set()
        if self.server is not None:
            self.server.join(1.0)
        self.adapter.close()
        self.assertFalse(self.server and self.server.is_alive())
        self.assertEqual(self.errors, [])
        actual = termios.tcgetattr(self.slave)
        expected = list(self.original)
        actual[3] &= ~getattr(termios, "PENDIN", 0)
        expected[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(actual, expected)

    def start_server(self, *, fault_command: str | None = None, fault: bytes | None = None,
                     late: bool = False, fragment: bool = False) -> None:
        def write(data: bytes) -> None:
            view = memoryview(data)
            while view:
                count = os.write(self.master, view)
                if not count:
                    raise OSError("synthetic PTY write returned zero")
                view = view[count:]

        def serve() -> None:
            pending = bytearray()
            try:
                while not self.stopped.is_set():
                    if not select.select([self.master], [], [], 0.01)[0]:
                        continue
                    pending.extend(os.read(self.master, 4096))
                    while b"\r" in pending:
                        raw, _, rest = pending.partition(b"\r")
                        pending[:] = rest
                        command = raw.decode("ascii")
                        self.commands.append(command)
                        if command == fault_command:
                            if fault:
                                write(fault)
                            if late:
                                if not self.release_late.wait(2.0):
                                    raise TimeoutError("test did not release late prompt")
                                write(b"ch> ")
                                self.late_sent.set()
                            continue
                        reply = IDENTITY_REPLIES.get(command)
                        if command == SCAN_COMMAND:
                            reply = "ch>\r\n" + command + "\r\n" + SCAN_REPLY
                        elif command in SETUP_COMMANDS:
                            reply = command + "\r\nch> "
                        if reply is None:
                            raise AssertionError(f"Unexpected synthetic command: {command}")
                        encoded = reply.encode("ascii")
                        if fragment:
                            # Split both the body and terminal prompt across
                            # actual wire writes within the quiet deadline.
                            for chunk in (encoded[:-4], b"c", b"h", b"> "):
                                write(chunk)
                                time.sleep(0.005)
                        else:
                            write(encoded)
            except Exception as exc:
                self.errors.append(exc)
        self.server = threading.Thread(target=serve, daemon=True)
        self.server.start()

    def test_fragmented_identity_setup_and_sweep_preserve_provenance(self) -> None:
        self.start_server(fragment=True)
        sweep = self.adapter.poll()
        self.assertEqual(self.commands, [*IDENTITY_COMMANDS, *SETUP_COMMANDS, SCAN_COMMAND])
        self.assertEqual(sweep.levels, (-90.0, -42.5, -80.0))
        self.assertEqual(sweep.settings["generator_output"], "off")
        self.assertEqual(sweep.settings["generator_output_source"], "command_acknowledged")
        self.assertFalse(sweep.settings["generator_output_readback"])
        self.assertEqual(sweep.settings["hardware_validation"], "not_run")

    def test_rejected_output_off_stops_before_mode_and_scan(self) -> None:
        self.start_server(fault_command="output off", fault=b"ERROR: command rejected\r\nch>")
        with self.assertRaisesRegex(WorkerError, "output off.*перезапуск"):
            self.adapter.poll()
        self.assertEqual(self.commands, [*IDENTITY_COMMANDS, "output off"])
        before = list(self.commands)
        for _ in range(2):
            with self.assertRaisesRegex(WorkerError, "перезапуск"):
                self.adapter.poll()
        self.assertEqual(self.commands, before)
        self.assertEqual(self.adapter.details()["generator_output"], "unknown")
        self.assertEqual(self.adapter.details()["generator_output_source"], "unknown")

    def assert_late_prompt_untouched(self, command: str, partial: bytes | None) -> None:
        self.start_server(fault_command=command, fault=partial, late=True)
        original_read = self.transport.read_until_quiet
        def bounded_read(quiet: float, maximum: float, label: str) -> str:
            if label == command:
                quiet, maximum = 0.01, 0.05
            return original_read(quiet, maximum, label)
        with patch.object(self.transport, "read_until_quiet", side_effect=bounded_read):
            with self.assertRaisesRegex(WorkerError, command + ".*перезапуск"):
                self.adapter.poll()
        self.release_late.set()
        self.assertTrue(self.late_sent.wait(1.0))
        before = list(self.commands)
        self.assertEqual(before, [*IDENTITY_COMMANDS, *SETUP_COMMANDS[:SETUP_COMMANDS.index(command) + 1]])
        for _ in range(2):
            with self.assertRaisesRegex(WorkerError, "перезапуск"):
                self.adapter.poll()
        self.assertEqual(self.commands, before)
        self.assertTrue(select.select([self.transport._fd], [], [], 0.5)[0])
        self.assertEqual(os.read(self.transport._fd, 4096), b"ch> ")
        self.assertEqual(self.adapter.details()["generator_output"], "unknown")
        self.assertTrue(self.adapter.details()["restart_required"])

    def test_timeout_after_partial_setup_never_consumes_late_prompt(self) -> None:
        self.assert_late_prompt_untouched("rbw 10", None)

    def test_echo_without_prompt_never_confirms_setup(self) -> None:
        self.assert_late_prompt_untouched("output off", b"output off\r\n")

    def test_partial_prompt_never_confirms_last_setup_step(self) -> None:
        self.assert_late_prompt_untouched("calc off", b"calc off\r\nch")


if __name__ == "__main__":
    unittest.main()
