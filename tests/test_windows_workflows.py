"""Проверка payload и команд рабочих сценариев Windows GUI."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from radioinspector.windows.app import WindowsApp
from radioinspector.windows.client import PlatformSnapshot
from radioinspector.windows.completion_app import CompletionWindowsApp
from radioinspector.windows.workflows import (
    automatic_completion_payload,
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
        with self.assertRaisesRegex(ValueError, "не меньше 0"):
            investigation_payload(
                target_stream_id="ar/dv1", target_frequency_hz=1, priority=-1
            )

    def test_detection_policy_is_explicit_bounded_and_dbm_only(self) -> None:
        payload = automatic_detection_payload(
            enabled=True,
            interval_seconds="0.5",
            stream_id=" tiny/sa ",
            threshold_dbm="-72.5",
            dedup_bandwidth_hz="125000",
            cooldown_seconds="0",
            max_candidates_per_sweep="4",
        )
        self.assertEqual(
            payload,
            {
                "enabled": True,
                "interval_seconds": 0.5,
                "stream_id": "tiny/sa",
                "threshold_dbm": -72.5,
                "dedup_bandwidth_hz": 125000,
                "cooldown_seconds": 0.0,
                "max_candidates_per_sweep": 4,
            },
        )
        self.assertNotIn("receiver_stream_id", payload)
        self.assertNotIn("minimum_priority", payload)
        disabled = automatic_detection_payload(
            **{**payload, "enabled": False, "stream_id": ""}
        )
        self.assertFalse(disabled["enabled"])
        self.assertIsNone(disabled["stream_id"])
        with self.assertRaisesRegex(ValueError, "boolean"):
            automatic_detection_payload(**{**payload, "enabled": 1})
        with self.assertRaisesRegex(ValueError, "диапазоне"):
            automatic_detection_payload(**{**payload, "threshold_dbm": "nan"})
        with self.assertRaisesRegex(ValueError, "не больше 100"):
            automatic_detection_payload(
                **{**payload, "max_candidates_per_sweep": 101}
            )

    def test_automatic_completion_payload_validates_types_and_ranges(self) -> None:
        payload = automatic_completion_payload(
            enabled=True,
            interval_seconds="0.5",
            minimum_listening_seconds="10",
            minimum_observations="2",
            receiver_stream_id="ar/dv1",
        )
        self.assertEqual(
            payload,
            {
                "enabled": True,
                "interval_seconds": 0.5,
                "minimum_listening_seconds": 10.0,
                "minimum_observations": 2,
                "receiver_stream_id": "ar/dv1",
            },
        )
        self.assertNotIn("minimum_priority", payload)
        self.assertNotIn("threshold_dbm", payload)
        disabled = automatic_completion_payload(
            **{**payload, "enabled": False, "receiver_stream_id": ""}
        )
        self.assertFalse(disabled["enabled"])
        self.assertIsNone(disabled["receiver_stream_id"])
        saved = automatic_completion_payload(
            **{**payload, "enabled": None, "receiver_stream_id": ""}
        )
        self.assertNotIn("enabled", saved)
        self.assertEqual(saved["minimum_observations"], 2)
        self.assertIsNone(saved["receiver_stream_id"])
        with self.assertRaisesRegex(ValueError, "boolean"):
            automatic_completion_payload(**{**payload, "enabled": 1})
        with self.assertRaisesRegex(ValueError, "диапазоне"):
            automatic_completion_payload(**{**payload, "interval_seconds": "nan"})
        with self.assertRaisesRegex(ValueError, "не меньше 1"):
            automatic_completion_payload(**{**payload, "minimum_observations": 0})

    def test_handoff_policy_is_independent_and_validated(self) -> None:
        payload = automatic_handoff_payload(
            enabled=True,
            interval_seconds="2",
            minimum_priority="5",
            receiver_stream_id="ar/dv1",
            max_consecutive_failures="3",
            retry_delay_seconds="30",
        )
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["minimum_priority"], 5)
        self.assertEqual(payload["receiver_stream_id"], "ar/dv1")
        self.assertNotIn("threshold_dbm", payload)
        self.assertNotIn("stream_id", payload)
        with self.assertRaisesRegex(ValueError, "не меньше 0"):
            automatic_handoff_payload(**{**payload, "minimum_priority": -1})
        with self.assertRaisesRegex(ValueError, "диапазоне"):
            automatic_handoff_payload(**{**payload, "retry_delay_seconds": 0})


class WorkflowCommandTests(unittest.TestCase):
    def test_commands_capture_ids_and_explicit_confirmation_before_dispatch(self) -> None:
        class RecordingClient:
            def __init__(self):
                self.calls = []

            def dismiss_candidate(self, session_id, candidate_id):
                self.calls.append(("dismiss", session_id, candidate_id))

            def start_next_investigation(self, session_id, investigation_id, *, confirm):
                self.calls.append(
                    ("next", session_id, investigation_id, confirm)
                )

            def fail_investigation(self, session_id, investigation_id, *, error):
                self.calls.append(("fail", session_id, investigation_id, error))

        class Tree:
            def __init__(self, selected):
                self.selected = selected

            def selection(self):
                return (self.selected,)

        class Value:
            def get(self):
                return "  Причина оператора  "

        app = WindowsApp.__new__(WindowsApp)
        app.client = RecordingClient()
        app.session_id = "session/old"
        app._require_session = lambda: True
        app.snapshot = PlatformSnapshot(
            {},
            {},
            [],
            candidates=[{"id": "candidate/old", "state": "new"}],
            investigations=[{"id": "investigation/old", "state": "listening"}],
            next_investigation={"id": "investigation/old"},
        )
        app.candidates_tree = Tree("candidate/old")
        app.investigations_tree = Tree("investigation/old")
        app.failure_var = Value()
        operations = []
        app._submit_action = lambda _description, operation: operations.append(operation)
        app._dismiss_candidate()
        app._start_next()
        app._investigation_action("fail")
        app.session_id = "session/new"
        app.snapshot = PlatformSnapshot({}, {}, [])
        for operation in operations:
            operation()
        self.assertEqual(
            app.client.calls,
            [
                ("dismiss", "session/old", "candidate/old"),
                ("next", "session/old", "investigation/old", True),
                ("fail", "session/old", "investigation/old", "Причина оператора"),
            ],
        )

    def test_disabling_one_policy_captures_session_and_does_not_touch_the_other(self) -> None:
        class RecordingClient:
            def __init__(self):
                self.calls = []

            def configure_automatic_detection(self, session_id, payload):
                self.calls.append(("detection", session_id, payload))

            def configure_automatic_handoff(self, session_id, payload):
                self.calls.append(("handoff", session_id, payload))

        app = WindowsApp.__new__(WindowsApp)
        app.client = RecordingClient()
        app.session_id = "session/old"
        app._require_session = lambda: True
        operations = []
        app._submit_action = lambda _description, operation: operations.append(operation)
        app._disable_policy("detection")
        app._disable_policy("handoff")
        app.session_id = "session/new"
        for operation in operations:
            operation()
        self.assertEqual(
            app.client.calls,
            [
                ("detection", "session/old", {"enabled": False}),
                ("handoff", "session/old", {"enabled": False}),
            ],
        )

    def test_completion_status_is_bound_to_a_visible_label(self) -> None:
        class Variable:
            def __init__(self, *, value):
                self.value = value

        class Widget:
            created = []

            def __init__(self, master, **kwargs):
                self.master = master
                self.kwargs = kwargs
                self.grid_kwargs = None
                self.created.append(self)

            def grid(self, **kwargs):
                self.grid_kwargs = kwargs

        class Tk:
            StringVar = Variable

        class Ttk:
            Label = Widget
            Button = Widget

        policies = object()
        app = CompletionWindowsApp.__new__(CompletionWindowsApp)
        app.tk = Tk()
        app.ttk = Ttk()
        app.detection_policy_button = Widget(policies)
        app._build_completion_controls()
        label, button = Widget.created[1:]
        self.assertIs(label.master, policies)
        self.assertIs(label.kwargs["textvariable"], app.completion_var)
        self.assertEqual(label.grid_kwargs["row"], 3)
        self.assertEqual(label.grid_kwargs["sticky"], "ew")
        self.assertIs(button.master, policies)
        self.assertEqual(button.kwargs["state"], "disabled")

    def test_installed_windows_launcher_uses_completion_app(self) -> None:
        project = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with project.open("rb") as stream:
            scripts = tomllib.load(stream)["project"]["scripts"]
        self.assertEqual(
            scripts["radioinspector-windows"],
            "radioinspector.windows.completion_app:main",
        )

    def test_disabling_completion_uses_only_completion_api(self) -> None:
        class RecordingClient:
            def __init__(self):
                self.calls = []

            def configure_automatic_completion(self, session_id, payload):
                self.calls.append(("completion", session_id, payload))

        app = CompletionWindowsApp.__new__(CompletionWindowsApp)
        app.client = RecordingClient()
        app.session_id = "session/old"
        app._require_session = lambda: True
        operations = []
        app._submit_action = lambda _description, operation: operations.append(operation)
        app._disable_policy("completion")
        app.session_id = "session/new"
        for operation in operations:
            operation()
        self.assertEqual(
            app.client.calls,
            [("completion", "session/old", {"enabled": False})],
        )

    def test_fill_uses_domain_ids_and_preserves_operator_selection(self) -> None:
        class Tree:
            def __init__(self):
                self.rows = {}
                self.selected = ()

            def selection(self):
                return self.selected

            def get_children(self):
                return tuple(self.rows)

            def delete(self, *ids):
                for item_id in ids:
                    self.rows.pop(item_id, None)

            def insert(self, _parent, _position, *, iid, values):
                self.rows[iid] = values

            def exists(self, item_id):
                return item_id in self.rows

            def selection_set(self, item_id):
                self.selected = (item_id,)

        tree = Tree()
        records = [{"id": "candidate/1"}, {"id": "candidate/2"}]
        formatter = lambda record: (record["id"],)
        WindowsApp._fill(tree, records, formatter)
        tree.selection_set("candidate/2")
        WindowsApp._fill(tree, list(reversed(records)), formatter)
        self.assertEqual(tree.selection(), ("candidate/2",))
        self.assertEqual(set(tree.rows), {"candidate/1", "candidate/2"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
