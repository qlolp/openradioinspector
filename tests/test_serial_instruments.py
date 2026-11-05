"""Scripted-проверки явных serial-адаптеров без физических портов."""

from __future__ import annotations

import time
import unittest
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
            "RF0433.07500": ["20"],
            "LM": ["20LM0551", "20LM1043"],
            "EX": ["20"],
        })


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
