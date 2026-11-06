"""Проверка payload и команд рабочих сценариев Windows GUI."""

from __future__ import annotations

import unittest

from radioinspector.windows.app import WindowsApp
from radioinspector.windows.client import PlatformSnapshot
from radioinspector.windows.workflows import (
    automatic_detection_payload,
    automatic_handoff_payload,
    candidate_payload,
    investigation_payload,
    session_payload,
)


class WorkflowPayloadTests(unittest.TestCase):
    def session(self, **changes):
        values = {
            "name": "  Двойной обзор  ",
            "tinysa_port": "sim://tinysa",
            "receiver_port": "sim://ardv1",
            "start_hz": "88000000",
            "stop_hz": "108000000",
            "receiver_frequency_hz": "145500000",
            "mode": " FM ",
            "simulated": True,
        }
        values.update(changes)
        return session_payload(**values)

    def test_session_adapter_is_explicit_and_profile_does_not_claim_hardware(self) -> None:
        payload = self.session()
        self.assertEqual(payload["name"], "Двойной обзор")
        self.assertEqual(payload["streams"][0]["level_unit"], "dBm")
        self.assertEqual(payload["streams"][1]["level_unit"], "s_meter_count")
        self.assertEqual(payload["streams"][0]["settings"]["adapter"], "tinysa-sim")
        self.assertEqual(payload["streams"][1]["settings"]["adapter"], "ardv1-sim")
        self.assertTrue(payload["metadata"]["simulated"])

        profile = self.session(
            tinysa_port="COM3", receiver_port="COM4", simulated=False
        )
        self.assertNotIn("adapter", profile["streams"][0]["settings"])
        self.assertNotIn("adapter", profile["streams"][1]["settings"])
        self.assertFalse(profile["metadata"]["simulated"])
        with self.assertRaisesRegex(ValueError, "должны различаться"):
            self.session(receiver_port="sim://tinysa")
        with self.assertRaisesRegex(ValueError, "больше начальной"):
            self.session(stop_hz="88000000")
        with self.assertRaisesRegex(ValueError, "целым числом"):
            self.session(start_hz="88.5")

    def test_serial_profile_is_explicit_and_validated(self) -> None:
        payload = self.session(
            tinysa_port="COM3",
            receiver_port="COM4",
            simulated=False,
            serial=True,
        )
        self.assertEqual(payload["streams"][0]["settings"]["adapter"], "tinysa-serial")
        self.assertEqual(payload["streams"][1]["settings"]["adapter"], "ardv1-serial")
        self.assertEqual(payload["streams"][0]["port"], "COM3")
        self.assertEqual(payload["streams"][1]["port"], "COM4")
        self.assertTrue(payload["metadata"]["serial"])
        self.assertFalse(payload["metadata"]["simulated"])
        self.assertEqual(payload["streams"][0]["settings"]["start_hz"], 88_000_000)
        self.assertEqual(payload["streams"][0]["settings"]["stop_hz"], 108_000_000)
        with self.assertRaisesRegex(ValueError, "sim://"):
            self.session(tinysa_port="sim://tinysa", simulated=False, serial=True)
        with self.assertRaisesRegex(ValueError, "одновременно"):
            self.session(simulated=True, serial=True)
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.session(simulated=False, serial=1)

    def test_candidate_preserves_source_unit_interval_and_real_sweep_link(self) -> None:
        stream = {"id": "tiny/sa", "level_unit": "dBm"}
        payload = candidate_payload(
            stream,
            started_at="2026-11-05T10:00:00Z",
            ended_at="2026-11-05T10:00:01Z",
            center_frequency_hz="102500000",
            bandwidth_hz="100000",
            peak_level="-42.25",
            score="",
            sweep_id="sweep/1",
        )
        self.assertEqual(payload["center_frequency_hz"], 102_500_000)
        self.assertEqual(payload["peak_level"], -42.25)
        self.assertEqual(payload["level_unit"], "dBm")
        self.assertEqual(payload["observation_ids"], ["sweep/1"])
        self.assertNotIn("score", payload)

        receiver = candidate_payload(
            {"id": "ar/dv1", "level_unit": "s_meter_count"},
            started_at="2026-11-05T10:00:00Z",
            ended_at="2026-11-05T10:00:01Z",
            center_frequency_hz=145_500_000,
            peak_level=37,
        )
        self.assertEqual(receiver["level_unit"], "s_meter_count")
        self.assertEqual(receiver["peak_level"], 37.0)
        self.assertNotIn("level_dbm", receiver)
        with self.assertRaisesRegex(ValueError, "Начало интервала"):
            candidate_payload(
                stream,
                started_at=" ",
                ended_at="2026-11-05T10:00:01Z",
                center_frequency_hz=1,
            )

    def test_investigation_validation_and_optional_mode(self) -> None:
        payload = investigation_payload(
            target_stream_id=" ar/dv1 ",
            target_frequency_hz="102500000",
            priority="0",
            mode=" ",
        )
        self.assertEqual(
            payload,
            {
                "target_stream_id": "ar/dv1",
                "target_frequency_hz": 102_500_000,
                "priority": 0,
            },
        )
        payload = investigation_payload(
            target_stream_id="ar/dv1",
            target_frequency_hz="102500000",
            priority="7",
            mode=" DMR ",
        )
        self.assertEqual(payload["mode"], "DMR")
        self.assertEqual(payload["priority"], 7)
        with self.assertRaisesRegex(ValueError, "Приоритет"):
            investigation_payload(
                target_stream_id="ar/dv1",
                target_frequency_hz="102500000",
                priority="-1",
            )

    def test_automatic_detection_payload_validates_types_ranges_and_optional_stream(self) -> None:
        payload = automatic_detection_payload(
            enabled=True,
            interval_seconds="0.5",
            stream_id="tiny/sa",
            threshold_dbm="-65.5",
            dedup_bandwidth_hz="125000",
            cooldown_seconds="12.5",
            max_candidates_per_sweep="4",
        )
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["interval_seconds"], 0.5)
        self.assertEqual(payload["stream_id"], "tiny/sa")
        self.assertEqual(payload["threshold_dbm"], -65.5)
        self.assertEqual(payload["dedup_bandwidth_hz"], 125000)
        self.assertEqual(payload["cooldown_seconds"], 12.5)
        self.assertEqual(payload["max_candidates_per_sweep"], 4)
        payload = automatic_detection_payload(
            enabled=False,
            interval_seconds=1,
            stream_id=" ",
            threshold_dbm=-70,
            dedup_bandwidth_hz=100000,
            cooldown_seconds=0,
            max_candidates_per_sweep=1,
        )
        self.assertIsNone(payload["stream_id"])
        with self.assertRaisesRegex(ValueError, "boolean"):
            automatic_detection_payload(
                enabled=1,
                interval_seconds=1,
                stream_id=None,
                threshold_dbm=-70,
                dedup_bandwidth_hz=1000,
                cooldown_seconds=0,
                max_candidates_per_sweep=1,
            )
        with self.assertRaisesRegex(ValueError, "конечным"):
            automatic_detection_payload(
                enabled=True,
                interval_seconds=float("nan"),
                stream_id=None,
                threshold_dbm=-70,
                dedup_bandwidth_hz=1000,
                cooldown_seconds=0,
                max_candidates_per_sweep=1,
            )

    def test_automatic_handoff_payload_is_independent_and_validated(self) -> None:
        payload = automatic_handoff_payload(
            enabled=True,
            interval_seconds="0.5",
            minimum_priority="5",
            receiver_stream_id="ar/dv1",
            max_consecutive_failures="2",
            retry_delay_seconds="10",
        )
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["interval_seconds"], 0.5)
        self.assertEqual(payload["minimum_priority"], 5)
        self.assertEqual(payload["receiver_stream_id"], "ar/dv1")
        self.assertEqual(payload["max_consecutive_failures"], 2)
        self.assertEqual(payload["retry_delay_seconds"], 10.0)
        payload = automatic_handoff_payload(
            enabled=False,
            interval_seconds=1,
            minimum_priority=0,
            receiver_stream_id=" ",
            max_consecutive_failures=3,
            retry_delay_seconds=30,
        )
        self.assertIsNone(payload["receiver_stream_id"])
        with self.assertRaisesRegex(ValueError, "boolean"):
            automatic_handoff_payload(
                enabled=0,
                interval_seconds=1,
                minimum_priority=0,
                receiver_stream_id=None,
                max_consecutive_failures=3,
                retry_delay_seconds=30,
            )
        with self.assertRaisesRegex(ValueError, "Минимальный приоритет"):
            automatic_handoff_payload(
                enabled=True,
                interval_seconds=1,
                minimum_priority=-1,
                receiver_stream_id=None,
                max_consecutive_failures=3,
                retry_delay_seconds=30,
            )

    def test_form_session_and_policy_actions_use_frozen_session_id(self) -> None:
        class Client:
            def __init__(self):
                self.calls = []

            def create_session(self, payload):
                self.calls.append(payload)
                return dict(payload)

        class Variable:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        client = Client()
        app = WindowsApp.__new__(WindowsApp)
        app.client = client
        app.session_id = "session/old"
        app._submit_action = lambda _description, operation: operation()
        app.tk = type("Tk", (), {"StringVar": lambda self, value: Variable(value)})()
        app._open_session_form()
        app.session_id = "session/new"
        app._create_session()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["name"], "Двойной обзор")
        self.assertEqual(client.calls[0]["streams"][0]["settings"]["adapter"], "tinysa-sim")

    def test_disable_policy_sends_only_enabled_false_and_does_not_start_handoff(self) -> None:
        class Client:
            def __init__(self):
                self.calls = []

            def configure_automatic_detection(self, session_id, payload):
                self.calls.append(("detection", session_id, payload))
                return {"policy": payload, "runtime": {}}

            def configure_automatic_handoff(self, session_id, payload):
                self.calls.append(("handoff", session_id, payload))
                return {"policy": payload, "runtime": {}}

        client = Client()
        app = WindowsApp.__new__(WindowsApp)
        app.client = client
        app.session_id = "session/old"
        app.snapshot = PlatformSnapshot(
            {},
            {},
            [],
            session={"state": "running", "streams": []},
            automatic_detection={"policy": {"enabled": True}, "runtime": {}},
            automatic_handoff={"policy": {"enabled": True}, "runtime": {}},
        )
        app._submit_action = lambda _description, operation: operation()
        app.tk = type("Tk", (), {"StringVar": lambda self, value: Variable(value)})()
        app._open_policy_form("detection")
        app._open_policy_form("handoff")
        app._set_policy_enabled("detection", False)
        app._set_policy_enabled("handoff", False)
        self.assertEqual(
            client.calls,
            [
                ("detection", "session/old", {"enabled": False}),
                ("handoff", "session/old", {"enabled": False}),
            ],
        )


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


if __name__ == "__main__":
    unittest.main()
