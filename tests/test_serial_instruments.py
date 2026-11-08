"""Scripted-проверки явных serial-адаптеров без физических портов."""

from __future__ import annotations

import time
import unittest
from collections import deque
from datetime import datetime, timezone
from typing import Any

from radioinspector.contracts import DeviceRole
from radioinspector.device_workers import DeviceWorkerManager, UnsupportedAdapterError, WorkerError
from radioinspector.serial_instruments import (
    ARDV1SerialAdapter,
    TinySASerialAdapter,
    create_device_adapter,
)
from radioinspector.session_service import SessionService
from tests.test_device_workers import simulated_session_payload, wait_until


class ScriptedTransport:
    def __init__(self, responses: dict[str, list[str]] | None = None) -> None:
        self.responses = {key: list(value) for key, value in (responses or {}).items()}
        self.sent: list[bytes] = []
        self.closed = False

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def read_until_quiet(self, quiet: float, maximum: float, label: str) -> str:
        return self._reply(label)

    def read_line(self, timeout: float, label: str) -> str:
        return self._reply(label)

    def _reply(self, label: str) -> str:
        values = self.responses.get(label)
        if not values:
            raise WorkerError(f"Нет scripted-ответа для {label}")
        return values.pop(0)

    def close(self) -> None:
        self.closed = True


class TinySATransport(ScriptedTransport):
    @classmethod
    def valid(cls) -> "TinySATransport":
        scan = (
            "ch>\r\n"
            "100000000 -9.000000e+01 0.0\r\n"
            "100500000 -4.250000e+01 0.0\r\n"
            "101000000 -8.000000e+01 0.0\r\n"
            "ch> "
        )
        return cls({
            "info": ["tinySA Ultra ZS405"],
            "version": ["tinySA4_v1.4-235-gea71dfd"],
            "deviceid": ["test-id"],
            "output off": ["ch>"],
            "mode input": ["ch>"],
            "lna off": ["ch>"],
            "attenuate auto": ["ch>"],
            "rbw 10": ["ch>"],
            "calc off": ["ch>"],
            "scan 100M 101M 3 3": [scan],
        })


class ARDV1Transport(ScriptedTransport):
    @classmethod
    def valid(cls) -> "ARDV1Transport":
        return cls({
            "RE1": ["20"],
            "VR": ["20VR1805A"],
            "RN": ["20RN0952zz"],
            "VFA": ["20"],
            "MD0F0": ["20"],
            "RF0145.50000": ["20"],
            "RF0433.07500": ["20"],
            "LM": ["20LM0551", "20LM1043"],
            "EX": ["20"],
        })


class FIFOARDV1Transport(ScriptedTransport):
    """Wire-order responses. read_line never matches a reply by its label."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: deque[str | Exception] = deque()
        self.actual_frequency_hz: int | None = None
        self.delayed_rf_timeout = False
        self.reject_recovery_rf = False
        self.next_lm_reply: tuple[str | Exception, ...] | None = None

    def send(self, data: bytes) -> None:
        super().send(data)
        command = data.decode("ascii").rstrip("\r")
        if command == "VR":
            self.pending.append("20VR1805A")
        elif command == "RN":
            self.pending.append("20RNsynthetic")
        elif command.startswith("RF"):
            whole, fraction = command[2:].split(".")
            frequency_hz = int(whole) * 1_000_000 + int(fraction) * 10
            if frequency_hz == 434_000_000 and self.reject_recovery_rf:
                self.pending.append("50")
                return
            self.actual_frequency_hz = frequency_hz
            if frequency_hz == 433_075_000 and self.delayed_rf_timeout:
                # The device changed frequency; its ACK arrives only after the
                # first read timed out. It must not acknowledge a future VFA.
                self.pending.append(WorkerError("synthetic delayed RF timeout"))
            self.pending.append("20")
        elif command == "LM":
            self.pending.extend(self.next_lm_reply or ("20LM0551",))
            self.next_lm_reply = None
        else:
            self.pending.append("20")

    def read_line(self, timeout: float, label: str) -> str:
        if not self.pending:
            raise WorkerError("synthetic FIFO timeout")
        response = self.pending.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def serial_session(*, max_errors: int = 3) -> dict[str, Any]:
    payload = simulated_session_payload()
    payload["streams"][0]["port"] = "/dev/ttyFAKE0"
    payload["streams"][0]["settings"] = {
        "adapter": "tinysa-serial",
        "start_hz": 100_000_000,
        "stop_hz": 101_000_000,
        "points": 3,
        "rbw_hz": 10_000,
        "period_s": 0.01,
        "max_consecutive_errors": max_errors,
    }
    payload["streams"][1]["port"] = "/dev/ttyFAKE1"
    payload["streams"][1]["settings"] = {
        "adapter": "ardv1-serial",
        "frequency_hz": 145_500_000,
        "mode": "FM",
        "period_s": 0.01,
    }
    return payload


class SerialAdapterTests(unittest.TestCase):
    def test_tinysa_sweep_keeps_raw_dbm_and_safe_command_order(self) -> None:
        service = SessionService()
        session = service.create_session(serial_session())
        transport = TinySATransport.valid()
        adapter = TinySASerialAdapter(session.streams[0], transport=transport)
        sweep = adapter.poll()
        adapter.close()
        self.assertEqual(sweep.level_unit, "dBm")
        self.assertEqual(sweep.frequencies_hz, (100_000_000, 100_500_000, 101_000_000))
        self.assertEqual(sweep.levels, (-90.0, -42.5, -80.0))
        self.assertTrue(all(isinstance(item, int) for item in sweep.frequencies_hz))
        self.assertIs(sweep.interval.started_at.tzinfo, timezone.utc)
        self.assertFalse(sweep.settings["simulated"])
        self.assertEqual(sweep.settings["hardware_validation"], "not_run")
        commands = [item.decode() for item in transport.sent]
        self.assertEqual(commands[:3], ["info\r", "version\r", "deviceid\r"])
        self.assertEqual(
            commands[3:9],
            ["output off\r", "mode input\r", "lna off\r", "attenuate auto\r", "rbw 10\r", "calc off\r"],
        )
        self.assertEqual(commands[9], "scan 100M 101M 3 3\r")
        self.assertNotIn("output on", " ".join(commands))
        self.assertTrue(transport.closed)

    def test_tinysa_rejects_identity_and_bad_point_count(self) -> None:
        service = SessionService()
        session = service.create_session(serial_session())
        wrong = TinySATransport.valid()
        wrong.responses["info"] = ["other-device"]
        adapter = TinySASerialAdapter(session.streams[0], transport=wrong)
        with self.assertRaisesRegex(WorkerError, "tinySA Ultra"):
            adapter.poll()
        self.assertFalse(any(item.startswith(b"scan") for item in wrong.sent))
        adapter.close()

        short = TinySATransport.valid()
        short.responses["scan 100M 101M 3 3"] = ["100000000 -90 0\r\n101000000 -80 0\r\n"]
        adapter = TinySASerialAdapter(session.streams[0], transport=short)
        with self.assertRaisesRegex(WorkerError, "получено 2"):
            adapter.poll()
        adapter.close()

    def test_ardv1_tune_and_observation_keep_raw_units(self) -> None:
        service = SessionService()
        session = service.create_session(serial_session())
        transport = ARDV1Transport.valid()
        adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
        tuned = adapter.execute("tune", {"frequency_hz": 433_075_000, "mode": "FM"})
        self.assertEqual(tuned["frequency_hz"], 433_075_000)
        observation = adapter.poll()
        self.assertEqual(observation.level, 55)
        self.assertEqual(observation.level_unit, "s_meter_count")
        self.assertTrue(observation.squelch_open)
        self.assertIsNone(observation.decoded_mode)
        self.assertEqual(observation.frequency_hz, 433_075_000)
        self.assertEqual(observation.metadata["hardware_validation"], "not_run")
        self.assertFalse(observation.metadata["simulated"])
        adapter.close()
        self.assertIn(b"RF0433.07500\r", transport.sent)
        self.assertNotIn(b"RF0145.50000\r", transport.sent)
        self.assertEqual(transport.sent.count(b"RF0433.07500\r"), 1)
        self.assertEqual(transport.sent[-1], b"EX\r")
        self.assertTrue(transport.closed)

    def test_ardv1_rejects_digital_mode_and_bad_reply(self) -> None:
        service = SessionService()
        session = service.create_session(serial_session())
        transport = ARDV1Transport.valid()
        adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
        with self.assertRaisesRegex(WorkerError, "только FM и AM"):
            adapter.execute("tune", {"frequency_hz": 433_075_000, "mode": "DMR"})
        self.assertEqual(transport.sent, [])
        transport.responses["LM"] = ["50"]
        with self.assertRaisesRegex(WorkerError, "отклонил"):
            adapter.poll()
        adapter.close()

    def test_ardv1_initial_poll_tunes_before_lm_and_labels_frequency_source(self) -> None:
        session = SessionService().create_session(serial_session())
        transport = ARDV1Transport.valid()
        adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
        self.addCleanup(adapter.close)
        self.assertIsNone(adapter.details()["frequency_hz"])
        self.assertIsNone(adapter.details()["mode"])
        self.assertEqual(adapter.details()["requested_frequency_hz"], 145_500_000)
        self.assertEqual(adapter.details()["frequency_source"], "unconfirmed")
        observation = adapter.poll()
        self.assertEqual(transport.sent, [
            b"RE1\r", b"VR\r", b"RN\r", b"VFA\r", b"MD0F0\r",
            b"RF0145.50000\r", b"LM\r",
        ])
        self.assertEqual(observation.frequency_hz, 145_500_000)
        self.assertEqual(observation.mode, "FM")
        self.assertEqual(observation.metadata["frequency_source"], "command_acknowledged")
        self.assertEqual(observation.metadata["mode_source"], "command_acknowledged")
        self.assertFalse(observation.metadata["frequency_readback"])
        self.assertEqual(observation.metadata["hardware_validation"], "not_run")
        self.assertEqual(observation.metadata["tune_sequence"], 1)
        adapter.poll()
        self.assertEqual(transport.sent.count(b"RF0145.50000\r"), 1)

    def test_ardv1_am_initialization_uses_configured_mode(self) -> None:
        payload = serial_session()
        payload["streams"][1]["settings"]["mode"] = "AM"
        session = SessionService().create_session(payload)
        transport = ARDV1Transport.valid()
        transport.responses["MD0F1"] = ["20"]
        adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
        self.addCleanup(adapter.close)
        self.assertEqual(adapter.poll().mode, "AM")
        self.assertIn(b"MD0F1\r", transport.sent)
        self.assertNotIn(b"MD0F0\r", transport.sent)

    def test_ardv1_initial_tune_failure_never_emits_lm(self) -> None:
        session = SessionService().create_session(serial_session())
        for step in ("VFA", "MD0F0", "RF0145.50000"):
            with self.subTest(step=step):
                transport = ARDV1Transport.valid()
                transport.responses[step] = ["50"]
                adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
                self.addCleanup(adapter.close)
                with self.assertRaisesRegex(WorkerError, "отклонил"):
                    adapter.poll()
                with self.assertRaisesRegex(WorkerError, "перезапуск"):
                    adapter.poll()
                self.assertNotIn(b"LM\r", transport.sent)
                self.assertIsNone(adapter.details()["frequency_hz"])
                self.assertIsNone(adapter.details()["mode"])
                self.assertEqual(adapter.details()["frequency_source"], "unknown")

    def test_ardv1_requires_terminal_ack_not_echo_or_unrelated_response(self) -> None:
        session = SessionService().create_session(serial_session())
        for step in ("RE1", "VFA", "MD0F0", "RF0433.07500"):
            for reply in ("", "OK", step, "20" + step, "20LM0551", "21", "11", "30", "?"):
                with self.subTest(step=step, reply=reply):
                    transport = ARDV1Transport.valid()
                    transport.responses[step] = [reply]
                    adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
                    self.addCleanup(adapter.close)
                    with self.assertRaises(WorkerError):
                        adapter.execute("tune", {"frequency_hz": 433_075_000})
                    self.assertIsNone(adapter.details()["frequency_hz"])
                    self.assertEqual(adapter.details()["tune_sequence"], 0)
                    self.assertNotIn(b"LM\r", transport.sent)

    def test_ardv1_identity_failure_stops_exchange_and_requires_restart(self) -> None:
        session = SessionService().create_session(serial_session())
        for step in ("VR", "RN"):
            for responses in (["20" + step], ["20LM0551"], ["21" + step + "synthetic"], []):
                with self.subTest(step=step, responses=responses):
                    transport = ARDV1Transport.valid()
                    transport.responses[step] = responses.copy()
                    adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
                    self.addCleanup(adapter.close)
                    with self.assertRaisesRegex(WorkerError, "перезапуск"):
                        adapter.poll()
                    self.assertEqual(transport.sent[-1], (step + "\r").encode("ascii"))
                    before = list(transport.sent)
                    with self.assertRaisesRegex(WorkerError, "перезапуск"):
                        adapter.execute("tune", {"frequency_hz": 433_075_000})
                    with self.assertRaisesRegex(WorkerError, "перезапуск"):
                        adapter.poll()
                    adapter.close()
                    self.assertEqual(transport.sent, before)
                    self.assertTrue(transport.closed)
                    self.assertIsNone(adapter.details()["frequency_hz"])
                    self.assertTrue(adapter.details()["restart_required"])

    def test_ardv1_accepts_only_terminal_success_codes_for_tune(self) -> None:
        session = SessionService().create_session(serial_session())
        for reply in ("10", "20"):
            with self.subTest(reply=reply):
                transport = ARDV1Transport.valid()
                for step in ("RE1", "VFA", "MD0F0", "RF0433.07500"):
                    transport.responses[step] = [reply]
                adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
                self.addCleanup(adapter.close)
                result = adapter.execute("tune", {"frequency_hz": 433_075_000})
                self.assertEqual(result["frequency_source"], "command_acknowledged")
                self.assertEqual(result["frequency_hz"], 433_075_000)

    def test_ardv1_failed_retune_requires_new_transport(self) -> None:
        session = SessionService().create_session(serial_session())
        for step in ("VFA", "MD0F1", "RF0433.07500"):
            for responses in (["50"], []):
                with self.subTest(step=step, responses=responses):
                    transport = ARDV1Transport.valid()
                    adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
                    self.addCleanup(adapter.close)
                    adapter.poll()
                    transport.responses.update({
                        "VFA": ["20"], "MD0F1": ["20"], "RF0433.07500": ["20"],
                    })
                    transport.responses[step] = responses.copy()
                    with self.assertRaises(WorkerError):
                        adapter.execute("tune", {"frequency_hz": 433_075_000, "mode": "AM"})
                    self.assertIsNone(adapter.details()["frequency_hz"])
                    self.assertIsNone(adapter.details()["mode"])
                    self.assertEqual(adapter.details()["tune_sequence"], 1)
                    self.assertEqual(adapter.details()["requested_frequency_hz"], 433_075_000)
                    self.assertEqual(adapter.details()["requested_mode"], "AM")
                    before = len(transport.sent)
                    with self.assertRaisesRegex(WorkerError, "перезапуск"):
                        adapter.poll()
                    self.assertEqual(len(transport.sent), before)
                    transport.responses.update({
                        "VFA": ["20"], "MD0F1": ["20"], "RF0433.07500": ["20"],
                    })
                    with self.assertRaisesRegex(WorkerError, "перезапуск"):
                        adapter.execute("tune", {"frequency_hz": 433_075_000, "mode": "AM"})
                    self.assertEqual(len(transport.sent), before)
                    self.assertTrue(adapter.details()["restart_required"])
                    adapter.close()
                    self.assertEqual(len(transport.sent), before)
                    self.assertTrue(transport.closed)
                    replacement_transport = ARDV1Transport.valid()
                    replacement_transport.responses["MD0F1"] = ["20"]
                    replacement = ARDV1SerialAdapter(session.streams[1], transport=replacement_transport)
                    self.addCleanup(replacement.close)
                    replacement.execute("tune", {"frequency_hz": 433_075_000, "mode": "AM"})
                    observation = replacement.poll()
                    self.assertEqual(observation.frequency_hz, 433_075_000)
                    self.assertEqual(observation.mode, "AM")
                    self.assertEqual(observation.metadata["tune_sequence"], 1)
                    self.assertFalse(replacement.details()["restart_required"])

    def test_ardv1_delayed_rf_ack_cannot_shift_fifo_during_retry(self) -> None:
        session = SessionService().create_session(serial_session())
        transport = FIFOARDV1Transport()
        adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
        self.addCleanup(adapter.close)
        adapter.poll()
        transport.delayed_rf_timeout = True
        transport.reject_recovery_rf = True
        with self.assertRaisesRegex(WorkerError, "delayed RF timeout"):
            adapter.execute("tune", {"frequency_hz": 433_075_000})
        self.assertEqual(transport.actual_frequency_hz, 433_075_000)
        self.assertEqual(list(transport.pending), ["20"])
        before = list(transport.sent)
        with self.assertRaisesRegex(WorkerError, "перезапуск"):
            adapter.execute("tune", {"frequency_hz": 434_000_000})
        for _ in range(2):
            with self.assertRaisesRegex(WorkerError, "перезапуск"):
                adapter.poll()
        self.assertEqual(transport.sent, before)
        self.assertEqual(list(transport.pending), ["20"])
        self.assertIsNone(adapter.details()["frequency_hz"])
        self.assertIsNone(adapter.details()["mode"])
        self.assertEqual(adapter.details()["requested_frequency_hz"], 433_075_000)
        self.assertTrue(adapter.details()["restart_required"])
        adapter.close()
        self.assertEqual(transport.sent, before)
        self.assertTrue(transport.closed)

    def test_ardv1_lm_failure_taints_fifo_and_never_retimestamps_late_level(self) -> None:
        session = SessionService().create_session(serial_session())
        for bad_reply in (
            WorkerError("synthetic LM timeout"), OSError("synthetic LM read failure"),
            "20", "nonsense", "50", "21LM0551", "20LM+551", "20LM-551", "20LM５５1", "٢0LM0551",
        ):
            with self.subTest(reply=bad_reply):
                transport = FIFOARDV1Transport()
                adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
                self.addCleanup(adapter.close)
                first = adapter.poll()
                transport.next_lm_reply = (bad_reply, "20LM0991")
                with self.assertRaisesRegex(WorkerError, "перезапуск"):
                    adapter.poll()
                before = list(transport.sent)
                for _ in range(2):
                    with self.assertRaisesRegex(WorkerError, "перезапуск"):
                        adapter.poll()
                with self.assertRaisesRegex(WorkerError, "перезапуск"):
                    adapter.execute("tune", {"frequency_hz": 434_000_000})
                self.assertEqual(transport.sent, before)
                self.assertEqual(list(transport.pending), ["20LM0991"])
                self.assertEqual(adapter._sequence, first.metadata["sequence"])
                self.assertIsNone(adapter.details()["frequency_hz"])
                self.assertIsNone(adapter.details()["mode"])
                self.assertTrue(adapter.details()["restart_required"])

    def test_ardv1_partial_write_taints_connection_and_closed_adapter_rejects_io(self) -> None:
        session = SessionService().create_session(serial_session())
        transport = FIFOARDV1Transport()
        adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
        self.addCleanup(adapter.close)
        adapter.poll()
        original_send = transport.send
        def fail_after_send(data: bytes) -> None:
            original_send(data)
            raise OSError("synthetic uncertain write")
        transport.send = fail_after_send
        with self.assertRaisesRegex(WorkerError, "uncertain write.*перезапуск"):
            adapter.execute("tune", {"frequency_hz": 433_075_000})
        before = list(transport.sent)
        adapter.close()
        for operation in (adapter.poll, lambda: adapter.execute("tune", {"frequency_hz": 434_000_000})):
            with self.assertRaisesRegex(WorkerError, "перезапуск"):
                operation()
        self.assertEqual(transport.sent, before)

    def test_ardv1_invalid_frequency_is_rejected_before_io_or_state_change(self) -> None:
        session = SessionService().create_session(serial_session())
        for frequency in (True, "145500000", 145_500_000.0, None, 99_990, 1_300_000_010, 145_500_001):
            with self.subTest(frequency=frequency):
                transport = ARDV1Transport.valid()
                adapter = ARDV1SerialAdapter(session.streams[1], transport=transport)
                self.addCleanup(adapter.close)
                adapter.poll()
                before = list(transport.sent)
                with self.assertRaises(WorkerError):
                    adapter.execute("tune", {"frequency_hz": frequency, "mode": "AM"})
                self.assertEqual(transport.sent, before)
                self.assertEqual(adapter.details()["frequency_hz"], 145_500_000)
                self.assertEqual(adapter.details()["mode"], "FM")

    def test_ardv1_invalid_initial_frequency_does_not_open_transport(self) -> None:
        for frequency in (True, "145500000", 145_500_000.0, None, 145_500_001):
            with self.subTest(frequency=frequency):
                payload = serial_session()
                payload["streams"][1]["settings"]["frequency_hz"] = frequency
                session = SessionService().create_session(payload)
                def forbidden_factory(*args):
                    self.fail("Invalid configuration must not open a port")
                with self.assertRaises(WorkerError):
                    ARDV1SerialAdapter(session.streams[1], transport_factory=forbidden_factory)

    def test_explicit_adapter_selection_and_close(self) -> None:
        service = SessionService()
        missing = simulated_session_payload()
        del missing["streams"][0]["settings"]["adapter"]
        session = service.create_session(missing)
        with self.assertRaises(UnsupportedAdapterError):
            create_device_adapter(session.streams[0], transport=TinySATransport.valid())

        session = service.create_session(serial_session())
        self.assertIsInstance(
            create_device_adapter(session.streams[0], transport=TinySATransport.valid()),
            TinySASerialAdapter,
        )
        self.assertIsInstance(
            create_device_adapter(session.streams[1], transport=ARDV1Transport.valid()),
            ARDV1SerialAdapter,
        )
        self.assertIs(session.streams[0].role, DeviceRole.SPECTRUM_ANALYZER)

    def test_serial_failure_does_not_stop_other_worker(self) -> None:
        service = SessionService(history_limit=5000)
        manager = DeviceWorkerManager(service)
        session = service.create_session(serial_session(max_errors=1))
        service.start_session(session.id)
        tiny = TinySATransport.valid()
        tiny.responses["scan 100M 101M 3 3"] = ["100000000 -90 0\r\n"]
        receiver = ARDV1Transport.valid()
        receiver.responses["LM"] = ["20LM0551"] * 100
        original_factory = __import__("radioinspector.serial_instruments", fromlist=["PosixSerialTransport"]).PosixSerialTransport
        try:
            def factory(path: str, baud: int):
                return tiny if path == "/dev/ttyFAKE0" else receiver
            import radioinspector.serial_instruments as module
            module.PosixSerialTransport = factory
            manager.start_session(session.id)
            wait_until(
                lambda: len(service.list_observations(session.id)) >= 3,
                "AR-DV1 должен продолжить наблюдения при сбое tinySA",
            )
            statuses = {item["stream_id"]: item for item in manager.list_workers(session.id)}
            self.assertEqual(statuses[session.streams[0].id]["state"], "error")
            self.assertFalse(statuses[session.streams[0].id]["alive"])
            self.assertTrue(statuses[session.streams[1].id]["alive"])
            self.assertEqual(statuses[session.streams[1].id]["state"], "running")
        finally:
            manager.close()
            service.close()
            import radioinspector.serial_instruments as module
            module.PosixSerialTransport = original_factory


if __name__ == "__main__":
    unittest.main()
