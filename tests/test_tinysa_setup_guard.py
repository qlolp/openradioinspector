"""Fail-closed tinySA setup, using scripted FIFO only, never a device."""

from __future__ import annotations

import unittest
from collections import deque

from radioinspector.device_workers import DeviceWorker, WorkerError
from radioinspector.serial_instruments import TinySASerialAdapter
from radioinspector.session_service import SessionService
from tests.test_serial_instruments import serial_session
from tests.test_device_workers import wait_until


IDENTITY_COMMANDS = ("info", "version", "deviceid")
SETUP_COMMANDS = (
    "output off", "mode input", "lna off", "attenuate auto", "rbw 10", "calc off",
)
SCAN_COMMAND = "scan 100M 101M 3 3"
SCAN_REPLY = "100000000 -90 0\r\n100500000 -42.5 0\r\n101000000 -80 0\r\nch> "
IDENTITY_REPLIES = {
    "info": "tinySA Ultra ZS405\r\nch> ",
    "version": "tinySA4_v1.4-235-gea71dfd\r\nch> ",
    "deviceid": "synthetic-id\r\nch> ",
}


class FIFOTinySATransport:
    """Read responses in wire order; labels never select a matching reply."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.pending: deque[str | Exception] = deque()
        self.replies: dict[str, tuple[str | Exception, ...]] = {}
        self.closed = False
        self.fail_send: str | None = None

    def send(self, data: bytes) -> None:
        command = data.decode("ascii").rstrip("\r")
        self.sent.append(command)
        if command == self.fail_send:
            raise WorkerError("synthetic partial write")
        default = IDENTITY_REPLIES.get(command, SCAN_REPLY if command == SCAN_COMMAND else "ch> ")
        self.pending.extend(self.replies.get(command, (default,)))

    def read_until_quiet(self, quiet: float, maximum: float, label: str) -> str:
        if not self.pending:
            raise WorkerError("synthetic timeout")
        reply = self.pending.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self) -> None:
        self.closed = True


class TinySASetupGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService()
        self.addCleanup(self.service.close)
        self.session = self.service.create_session(serial_session())

    def adapter(self, transport: FIFOTinySATransport) -> TinySASerialAdapter:
        adapter = TinySASerialAdapter(self.session.streams[0], transport=transport)
        self.addCleanup(adapter.close)
        return adapter

    def test_rejected_output_off_cannot_scan_or_claim_generator_off(self) -> None:
        transport = FIFOTinySATransport()
        transport.replies["output off"] = ("ERROR: command rejected\r\nch>",)
        adapter = self.adapter(transport)
        with self.assertRaisesRegex(WorkerError, "output off"):
            adapter.poll()
        self.assertEqual(transport.sent, [*IDENTITY_COMMANDS, "output off"])
        self.assertEqual(adapter.details()["generator_output"], "unknown")
        self.assertEqual(adapter.details()["generator_output_source"], "unknown")
        self.assertFalse(adapter.details()["generator_output_readback"])
        self.assertTrue(adapter.details()["restart_required"])

    def test_generator_unknown_before_setup(self) -> None:
        adapter = self.adapter(FIFOTinySATransport())
        self.assertEqual(adapter.details()["generator_output"], "unknown")
        self.assertEqual(adapter.details()["generator_output_source"], "unconfirmed")
        self.assertFalse(adapter.details()["generator_output_readback"])
        self.assertFalse(adapter.details()["restart_required"])

    def test_every_setup_rejection_and_ambiguous_reply_stops_immediately(self) -> None:
        for index, command in enumerate(SETUP_COMMANDS):
            for response in (
                "ERROR: command rejected\r\nch>", "error\r\nch>",
                "unknown command\r\nch>", "usage: command\r\nch>",
                "OK\r\nch>", "output on\r\nch>", "ch>\r\nch>",
                "ch>\r\nERROR", "", " \r\n", command, command + "\r\nch",
                "ch>unexpected", "\x00ch>", "\x7f\r\nch>", "\ufffd\r\nch>",
                command + "\r\n" + command + "\r\nch>",
                "ch> " + command + "\r\nch>",
            ):
                with self.subTest(command=command, response=response):
                    transport = FIFOTinySATransport()
                    transport.replies[command] = (response,)
                    adapter = self.adapter(transport)
                    with self.assertRaisesRegex(WorkerError, command):
                        adapter.poll()
                    self.assertEqual(transport.sent, [*IDENTITY_COMMANDS, *SETUP_COMMANDS[:index + 1]])
                    self.assertEqual(adapter.details()["generator_output"], "unknown")
                    self.assertTrue(adapter.details()["restart_required"])
                    before = list(transport.sent)
                    for _ in range(2):
                        with self.assertRaisesRegex(WorkerError, "перезапуск"):
                            adapter.poll()
                    adapter.close()
                    self.assertEqual(transport.sent, before)

    def test_timeout_partial_response_and_late_prompt_do_not_retry(self) -> None:
        for command in SETUP_COMMANDS:
            for fault in (WorkerError("synthetic timeout"), command + "\r\n"):
                with self.subTest(command=command, fault=fault):
                    transport = FIFOTinySATransport()
                    transport.replies[command] = (fault, "ch> ")
                    adapter = self.adapter(transport)
                    with self.assertRaises(WorkerError):
                        adapter.poll()
                    before = list(transport.sent)
                    pending = list(transport.pending)
                    self.assertEqual(pending, ["ch> "])
                    for _ in range(2):
                        with self.assertRaisesRegex(WorkerError, "перезапуск"):
                            adapter.poll()
                    adapter.close()
                    self.assertEqual(transport.sent, before)
                    self.assertEqual(list(transport.pending), pending)
                    self.assertEqual(adapter.details()["generator_output"], "unknown")

    def test_setup_partial_write_taints_connection(self) -> None:
        for command in SETUP_COMMANDS:
            with self.subTest(command=command):
                transport = FIFOTinySATransport()
                transport.fail_send = command
                adapter = self.adapter(transport)
                with self.assertRaisesRegex(WorkerError, "partial write.*перезапуск"):
                    adapter.poll()
                before = list(transport.sent)
                with self.assertRaisesRegex(WorkerError, "перезапуск"):
                    adapter.poll()
                self.assertEqual(transport.sent, before)
                self.assertEqual(adapter.details()["generator_output"], "unknown")

    def test_only_terminal_prompt_and_optional_exact_echo_are_accepted(self) -> None:
        for echoed in (False, True):
            with self.subTest(echoed=echoed):
                transport = FIFOTinySATransport()
                if echoed:
                    for command in SETUP_COMMANDS:
                        transport.replies[command] = (command + "\r\nch> ",)
                adapter = self.adapter(transport)
                sweep = adapter.poll()
                self.assertEqual(transport.sent, [*IDENTITY_COMMANDS, *SETUP_COMMANDS, SCAN_COMMAND])
                self.assertEqual(sweep.levels, (-90.0, -42.5, -80.0))
                self.assertEqual(sweep.settings["generator_output"], "off")
                self.assertEqual(sweep.settings["generator_output_source"], "command_acknowledged")
                self.assertFalse(sweep.settings["generator_output_readback"])
                self.assertEqual(sweep.settings["hardware_validation"], "not_run")
                self.assertEqual(adapter.details()["generator_output"], "off")
                self.assertEqual(adapter.details()["generator_output_source"], "command_acknowledged")
                self.assertFalse(adapter.details()["generator_output_readback"])
                adapter.poll()
                self.assertEqual(transport.sent[-2:], [SCAN_COMMAND, SCAN_COMMAND])

    def test_partial_identity_never_leaves_prompt_for_setup(self) -> None:
        for command in IDENTITY_COMMANDS:
            with self.subTest(command=command):
                transport = FIFOTinySATransport()
                transport.replies[command] = (IDENTITY_REPLIES[command].removesuffix("ch> "), "ch> ")
                adapter = self.adapter(transport)
                with self.assertRaisesRegex(WorkerError, command):
                    adapter.poll()
                self.assertNotIn("output off", transport.sent)
                before = list(transport.sent)
                with self.assertRaisesRegex(WorkerError, "перезапуск"):
                    adapter.poll()
                self.assertEqual(transport.sent, before)
                self.assertEqual(list(transport.pending), ["ch> "])

    def test_scan_retains_legacy_opening_prompt_and_optional_echo(self) -> None:
        for echo in ("", SCAN_COMMAND + "\r\n"):
            with self.subTest(echo=echo):
                transport = FIFOTinySATransport()
                transport.replies[SCAN_COMMAND] = ("ch>\r\n" + echo + SCAN_REPLY,)
                sweep = self.adapter(transport).poll()
                self.assertEqual(sweep.levels, (-90.0, -42.5, -80.0))
                self.assertEqual(sweep.settings["generator_output_source"], "command_acknowledged")

    def test_scan_failure_clears_provenance_and_cannot_retimestamp_late_data(self) -> None:
        for response in (
            WorkerError("synthetic scan timeout"), SCAN_REPLY.removesuffix("ch> "),
            "100000000 -90 0\r\nch> ", "ERROR: scan rejected\r\nch> ",
            "ch>\r\nch> ", "ch>\r\nch>\r\n" + SCAN_REPLY,
            "ch>\r\n" + SCAN_REPLY.removesuffix("ch> "),
        ):
            with self.subTest(response=response):
                transport = FIFOTinySATransport()
                adapter = self.adapter(transport)
                adapter.poll()
                transport.replies[SCAN_COMMAND] = (response, SCAN_REPLY)
                with self.assertRaisesRegex(WorkerError, "перезапуск"):
                    adapter.poll()
                before = list(transport.sent)
                for _ in range(2):
                    with self.assertRaisesRegex(WorkerError, "перезапуск"):
                        adapter.poll()
                self.assertEqual(transport.sent, before)
                self.assertEqual(list(transport.pending), [SCAN_REPLY])
                self.assertEqual(adapter.details()["generator_output"], "unknown")
                self.assertEqual(adapter.details()["generator_output_source"], "unknown")
                self.assertTrue(adapter.details()["restart_required"])

    def test_closed_adapter_never_sends_setup_or_scan(self) -> None:
        transport = FIFOTinySATransport()
        adapter = self.adapter(transport)
        adapter.close()
        with self.assertRaisesRegex(WorkerError, "перезапуск"):
            adapter.poll()
        self.assertEqual(transport.sent, [])

    def test_worker_retries_never_resend_setup_or_archive_unconfirmed_sweep(self) -> None:
        transport = FIFOTinySATransport()
        transport.replies["mode input"] = (WorkerError("synthetic timeout"), "ch> ")
        adapter = self.adapter(transport)
        self.service.start_session(self.session.id)
        worker = DeviceWorker(self.service, adapter)
        self.addCleanup(worker.stop)
        worker.start()
        wait_until(lambda: not worker.alive, "Failed setup must terminate worker")
        status = worker.status()
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["consecutive_errors"], adapter.max_consecutive_errors)
        self.assertEqual(status["measurements"], 0)
        self.assertEqual(status["current"]["generator_output"], "unknown")
        self.assertEqual(status["current"]["generator_output_source"], "unknown")
        self.assertTrue(status["current"]["restart_required"])
        self.assertEqual(self.service.list_sweeps(self.session.id), [])
        self.assertEqual(transport.sent, [*IDENTITY_COMMANDS, "output off", "mode input"])
        self.assertEqual(list(transport.pending), ["ch> "])
        self.assertTrue(transport.closed)
        # Recovery requires a separate adapter/transport, with all setup again.
        fresh = FIFOTinySATransport()
        self.adapter(fresh).poll()
        self.assertEqual(fresh.sent, [*IDENTITY_COMMANDS, *SETUP_COMMANDS, SCAN_COMMAND])


if __name__ == "__main__":
    unittest.main()
