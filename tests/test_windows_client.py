"""Windows GUI: тонкий HTTP-клиент и проверка команд на общем стенде."""

from __future__ import annotations

import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

from radioinspector.windows.app import (
    WindowsApp,
    candidate_row,
    investigation_row,
    policy_text,
    session_label,
    worker_row,
)
from radioinspector.windows.client import (
    PlatformClient,
    PlatformClientError,
    PlatformSnapshot,
    validate_tune,
)
from radioinspector.web.server import create_server
from radioinspector.windows.workflows import (
    automatic_completion_payload,
    automatic_detection_payload,
    automatic_handoff_payload,
    session_payload as windows_session_payload,
)
from tests.test_device_workers import simulated_session_payload, wait_until
from tests.test_persistence import measurement_interval


class BrokenOpener:
    def open(self, _request, timeout):
        raise urllib.error.URLError("стенд недоступен")


class WindowsClientValidationTests(unittest.TestCase):
    def test_url_and_tune_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "http://"):
            PlatformClient("127.0.0.1:8770")
        with self.assertRaisesRegex(ValueError, "Учётные данные"):
            PlatformClient("http://user:secret@example.test")
        self.assertEqual(
            PlatformClient("http://example.test/root/api/v1/").base_url,
            "http://example.test/root",
        )
        self.assertEqual(validate_tune(433_075_000, " DMR "), (433_075_000, "DMR"))
        for frequency in (True, 100.5, 0, -1):
            with self.assertRaises(ValueError):
                validate_tune(frequency, "FM")
        with self.assertRaisesRegex(ValueError, "Режим"):
            validate_tune(100_000_000, " ")

    def test_transport_error_is_typed_and_does_not_open_devices(self) -> None:
        client = PlatformClient("http://example.test", opener=BrokenOpener())
        with self.assertRaises(PlatformClientError) as caught:
            client.health()
        self.assertEqual(caught.exception.code, "connection_error")
        self.assertIn("стенд недоступен", str(caught.exception))

    def test_ui_command_captures_session_and_stream_before_background_dispatch(self) -> None:
        class RecordingClient:
            def __init__(self):
                self.calls = []

            def start_session(self, session_id):
                self.calls.append(("start", session_id))

            def tune(self, session_id, stream_id, *, frequency_hz, mode):
                self.calls.append(("tune", session_id, stream_id, frequency_hz, mode))

        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        app = WindowsApp.__new__(WindowsApp)
        app.client = RecordingClient()
        app.session_id = "session/old"
        app._require_session = lambda: True
        operations = []
        app._submit_action = lambda _description, operation: operations.append(operation)
        app._session_action("start")
        app.selected_stream_id = lambda: "receiver/old"
        app._selected_stream = lambda: {"id": "receiver/old", "role": "receiver"}
        app.frequency_var = Value("433075000")
        app.mode_var = Value("DMR")
        app._tune()
        app.session_id = "session/new"
        for operation in operations:
            operation()
        self.assertEqual(
            app.client.calls,
            [
                ("start", "session/old"),
                ("tune", "session/old", "receiver/old", 433_075_000, "DMR"),
            ],
        )

    def test_stream_selection_enables_controls_before_their_first_use(self) -> None:
        class Button:
            def __init__(self):
                self.state = "disabled"

            def configure(self, *, state):
                self.state = state

        class Value:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        app = WindowsApp.__new__(WindowsApp)
        app.client = object()
        app.session_id = "session"
        app._action_running = False
        for name in (
            "session_start",
            "session_stop",
            "workers_start",
            "workers_stop",
            "stream_start",
            "stream_stop",
            "tune_button",
            "refresh_button",
        ):
            setattr(app, name, Button())
        app.snapshot = PlatformSnapshot(
            {},
            {},
            [],
            session={
                "id": "session",
                "streams": [
                    {
                        "id": "ar/dv1",
                        "role": "receiver",
                        "settings": {"frequency_hz": 145_500_000, "mode": "FM"},
                    }
                ]
            },
        )
        app.selected_stream_id = lambda: "ar/dv1"
        app.frequency_var = Value()
        app.mode_var = Value()
        app._render_spectrum = lambda: None
        app.refresh = lambda: None
        app._select_stream()
        self.assertEqual(app.stream_start.state, "normal")
        self.assertEqual(app.stream_stop.state, "normal")
        self.assertEqual(app.tune_button.state, "normal")
        self.assertEqual(app.frequency_var.value, "145500000")
        self.assertEqual(app.mode_var.value, "FM")

    def test_spectrum_stream_commands_do_not_enable_receiver_tune(self) -> None:
        class Button:
            def __init__(self):
                self.state = "disabled"

            def configure(self, *, state):
                self.state = state

        app = WindowsApp.__new__(WindowsApp)
        app.client = object()
        app.session_id = "session"
        app._action_running = False
        for name in (
            "session_start",
            "session_stop",
            "workers_start",
            "workers_stop",
            "stream_start",
            "stream_stop",
            "tune_button",
            "refresh_button",
        ):
            setattr(app, name, Button())
        app.snapshot = PlatformSnapshot(
            {},
            {},
            [],
            session={
                "id": "session",
                "streams": [
                    {
                        "id": "tiny/sa",
                        "role": "spectrum_analyzer",
                        "level_unit": "dBm",
                    }
                ]
            },
        )
        app.selected_stream_id = lambda: "tiny/sa"
        app._render_controls()
        self.assertEqual(app.stream_start.state, "normal")
        self.assertEqual(app.stream_stop.state, "normal")
        self.assertEqual(app.tune_button.state, "disabled")

    def test_snapshot_defaults_and_presentation_preserve_source_values(self) -> None:
        snapshot = PlatformSnapshot({}, {}, [])
        self.assertEqual(snapshot.workers, [])
        self.assertEqual(snapshot.candidates, [])
        self.assertEqual(snapshot.investigations, [])
        self.assertIn("draft", session_label({"name": "Тест", "state": "draft", "id": "s"}))
        row = worker_row(
            {"device": "tinySA Ultra", "role": "spectrum_analyzer", "level_unit": "dBm"},
            {"state": "degraded", "measurements": 7, "last_error": "временный сбой"},
        )
        self.assertEqual(row[:5], ("tinySA Ultra", "spectrum_analyzer", "degraded", "dBm", "7"))
        candidate = candidate_row(
            {
                "state": "new",
                "center_frequency_hz": 100_200_000,
                "bandwidth_hz": 100_000,
                "peak_level": -45.0,
                "level_unit": "dBm",
                "id": "candidate",
            }
        )
        self.assertEqual(candidate[3], "-45 dBm")
        investigation = investigation_row(
            {
                "state": "queued",
                "priority": 5,
                "target_frequency_hz": 100_200_000,
                "mode": "FM",
                "target_stream_id": "receiver",
            }
        )
        self.assertEqual(investigation[1], "5")
        self.assertIn("выключено", policy_text({"policy": {"enabled": False}, "runtime": {}}))


class WindowsClientIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = PlatformClient(f"http://{host}:{port}", timeout=3)
        payload = simulated_session_payload()
        payload["id"] = "windows/session"
        payload["streams"][0]["id"] = "tiny/sa"
        payload["streams"][1]["id"] = "ar/dv1"
        self.session = self.server.session_service.create_session(payload)
        self.tinysa_id, self.receiver_id = [item.id for item in self.session.streams]

    def tearDown(self) -> None:
        self.server.automatic_detection.close()
        self.server.automatic_handoff.close()
        self.server.worker_manager.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server.session_service.close()

    def workers(self):
        return {
            item["stream_id"]: item
            for item in self.server.worker_manager.list_workers(self.session.id)
        }

    def test_snapshot_and_encoded_ids(self) -> None:
        self.client.start_session(self.session.id)
        snapshot = self.client.snapshot(self.session.id)
        self.assertEqual(snapshot.session["id"], "windows/session")
        self.assertEqual(snapshot.session["state"], "running")
        self.assertEqual(len(snapshot.workers), 2)
        self.assertFalse(snapshot.automatic_detection["policy"]["enabled"])
        self.assertFalse(snapshot.automatic_handoff["policy"]["enabled"])
        self.assertFalse(snapshot.automatic_detection["policy_persistent"])
        self.assertTrue(snapshot.capabilities["features"]["automatic_candidate_detection"])
        with self.assertRaises(PlatformClientError) as caught:
            self.client.get_session("missing/session")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "session_not_found")

    def test_independent_stream_commands_and_tune(self) -> None:
        self.client.start_session(self.session.id)
        self.client.start_workers(self.session.id)
        wait_until(
            lambda: all(item["measurements"] >= 2 for item in self.workers().values()),
            "Оба симулятора должны начать независимую запись",
        )
        self.client.stop_stream(self.session.id, self.tinysa_id)
        stopped = self.workers()[self.tinysa_id]["measurements"]
        receiver_before = self.workers()[self.receiver_id]["measurements"]
        wait_until(
            lambda: self.workers()[self.receiver_id]["measurements"] > receiver_before,
            "Receiver должен продолжать запись после остановки tinySA",
        )
        current = self.workers()
        self.assertEqual(current[self.tinysa_id]["measurements"], stopped)
        self.assertGreater(current[self.receiver_id]["measurements"], receiver_before)

        tuned = self.client.tune(
            self.session.id,
            self.receiver_id,
            frequency_hz=433_075_000,
            mode="DMR",
        )
        self.assertEqual(tuned["tuned"]["frequency_hz"], 433_075_000)
        self.assertEqual(tuned["tuned"]["mode"], "DMR")
        # Tune ACK updates settings before the next acquisition is committed.
        wait_until(
            lambda: (
                self.workers()[self.receiver_id]["current"]["frequency_hz"] == 433_075_000
                and any(
                    item.frequency_hz == 433_075_000
                    for item in self.server.session_service.list_observations(self.session.id)
                )
            ),
            "Receiver должен выполнить tune через собственную очередь",
        )
        observations = self.server.session_service.list_observations(self.session.id)
        self.assertTrue(any(item.frequency_hz == 433_075_000 for item in observations))
        self.assertTrue(all(item.level_unit == "s_meter_count" for item in observations))

        self.client.start_stream(self.session.id, self.tinysa_id)
        wait_until(
            lambda: self.workers()[self.tinysa_id]["measurements"] > stopped,
            "tinySA должен перезапуститься отдельно от receiver",
        )
        self.client.stop_session(self.session.id)
        self.assertEqual(self.client.get_session(self.session.id)["state"], "stopped")
        self.assertTrue(all(not item["alive"] for item in self.workers().values()))

    def test_tune_wait_requires_observation_commit_after_configuration_ack(self) -> None:
        commit_entered = threading.Event()
        commit_allowed = threading.Event()
        original_ingest = self.server.session_service.ingest_observation
        original_wait = wait_until

        def held_ingest(observation):
            if observation.frequency_hz == 433_075_000:
                commit_entered.set()
                commit_allowed.wait()
            return original_ingest(observation)

        def checked_wait(predicate, message, timeout=original_wait.__defaults__[0]):
            if message != "Receiver должен выполнить tune через собственную очередь":
                return original_wait(predicate, message, timeout)
            deadline = time.monotonic() + timeout
            try:
                original_wait(commit_entered.is_set, "Owned post-tune ingest was not reached", timeout)
                current = self.workers()[self.receiver_id]["current"]
                self.assertEqual(current["frequency_hz"], 433_075_000)
                self.assertEqual(current["mode"], "DMR")
                self.assertFalse(
                    any(
                        item.frequency_hz == 433_075_000
                        for item in self.server.session_service.list_observations(self.session.id)
                    )
                )
                self.assertFalse(
                    predicate(), "Configuration ACK cannot satisfy the observation-commit wait"
                )
            finally:
                commit_allowed.set()
            return original_wait(predicate, message, max(0.0, deadline - time.monotonic()))

        try:
            with patch.object(
                self.server.session_service, "ingest_observation", side_effect=held_ingest
            ):
                with patch(__name__ + ".wait_until", side_effect=checked_wait):
                    self.test_independent_stream_commands_and_tune()
            self.assertTrue(commit_entered.is_set())
        finally:
            # Always unblock this same owned simulator before tearDown joins it.
            commit_allowed.set()

    def test_snapshot_exposes_candidates_queue_and_does_not_start_handoff(self) -> None:
        self.client.start_session(self.session.id)
        candidate = self.server.session_service.create_candidate(
            self.session.id,
            {
                "source_stream_id": self.tinysa_id,
                "interval": measurement_interval(1),
                "center_frequency_hz": 100_200_000,
                "bandwidth_hz": 100_000,
                "peak_level": -45.0,
                "level_unit": "dBm",
                "score": 15.0,
            },
        )
        investigation = self.server.session_service.enqueue_investigation(
            self.session.id,
            candidate.id,
            {"target_stream_id": self.receiver_id, "mode": "FM", "priority": 5},
        )
        snapshot = self.client.snapshot(self.session.id)
        self.assertEqual(len(snapshot.candidates), 1)
        self.assertEqual(snapshot.candidates[0]["level_unit"], "dBm")
        self.assertEqual(len(snapshot.investigations), 1)
        self.assertEqual(snapshot.investigations[0]["id"], investigation.id)
        self.assertEqual(snapshot.investigations[0]["state"], "queued")
        self.assertEqual(snapshot.next_investigation["id"], investigation.id)
        self.assertFalse(self.workers()[self.receiver_id]["alive"])

    def test_spectrum_snapshot_returns_real_dbm_sweeps_with_encoded_stream_id(self) -> None:
        self.client.start_session(self.session.id)
        self.client.start_workers(self.session.id)
        wait_until(
            lambda: len(self.server.session_service.list_sweeps(self.session.id)) >= 2,
            "tinySA должен записать реальные симулированные sweeps",
        )
        snapshot = self.client.snapshot(
            self.session.id, spectrum_stream_id=self.tinysa_id
        )
        self.assertEqual(snapshot.spectrum_stream_id, "tiny/sa")
        self.assertGreaterEqual(len(snapshot.sweeps), 1)
        self.assertLessEqual(len(snapshot.sweeps), 40)
        self.assertTrue(
            all(item["stream_id"] == self.tinysa_id for item in snapshot.sweeps)
        )
        self.assertTrue(
            all(item["level_unit"] == "dBm" for item in snapshot.sweeps)
        )
        latest = snapshot.sweeps[-1]
        self.assertTrue(
            all(isinstance(value, int) for value in latest["frequencies_hz"])
        )
        archived = next(
            item
            for item in self.server.session_service.list_sweeps(
                self.session.id, stream_id=self.tinysa_id, limit=1000
            )
            if item.id == latest["id"]
        )
        self.assertEqual(latest["levels"], list(archived.levels))
        without_spectrum = self.client.snapshot(self.session.id)
        self.assertEqual(without_spectrum.sweeps, [])
        self.assertIsNone(without_spectrum.spectrum_stream_id)
        with self.assertRaises(ValueError):
            self.client.list_sweeps(self.session.id, self.tinysa_id, limit=0)
        with self.assertRaises(ValueError):
            self.client.snapshot(spectrum_stream_id=self.tinysa_id)
        self.client.stop_session(self.session.id)

    def test_create_session_through_client_keeps_simulator_opt_in(self) -> None:
        payload = windows_session_payload(
            name="Создано Windows-клиентом",
            tinysa_port="sim://created-tinysa",
            receiver_port="sim://created-ardv1",
            start_hz=88_000_000,
            stop_hz=108_000_000,
            receiver_frequency_hz=145_500_000,
            mode="FM",
            simulated=True,
        )
        payload["id"] = "created/windows"
        payload["streams"][0]["id"] = "created/tiny"
        payload["streams"][1]["id"] = "created/ar"
        created = self.client.create_session(payload)
        self.assertEqual(created["id"], "created/windows")
        self.assertEqual(created["state"], "draft")
        self.assertEqual(created["streams"][0]["settings"]["adapter"], "tinysa-sim")
        self.assertEqual(created["streams"][1]["level_unit"], "s_meter_count")
        self.assertTrue(self.client.capabilities()["features"]["hardware_control"])
        self.assertTrue(self.client.capabilities()["features"]["hardware_validation_not_run"])
        self.assertEqual(
            self.client.get_session("created/windows")["name"],
            "Создано Windows-клиентом",
        )

    def test_policy_editing_has_independent_opt_in_and_encoded_session_id(self) -> None:
        self.client.start_session(self.session.id)
        detection = automatic_detection_payload(
            enabled=True,
            interval_seconds=0.5,
            stream_id=self.tinysa_id,
            threshold_dbm=-65,
            dedup_bandwidth_hz=125000,
            cooldown_seconds=10,
            max_candidates_per_sweep=4,
        )
        detection_status = self.client.configure_automatic_detection(
            self.session.id, detection
        )
        self.assertTrue(detection_status["policy"]["enabled"])
        self.assertEqual(detection_status["policy"]["stream_id"], self.tinysa_id)
        self.assertFalse(
            self.client.automatic_handoff(self.session.id)["policy"]["enabled"]
        )

        handoff = automatic_handoff_payload(
            enabled=True,
            interval_seconds=0.5,
            minimum_priority=5,
            receiver_stream_id=self.receiver_id,
            max_consecutive_failures=2,
            retry_delay_seconds=10,
        )
        handoff_status = self.client.configure_automatic_handoff(
            self.session.id, handoff
        )
        self.assertTrue(handoff_status["policy"]["enabled"])
        self.assertEqual(handoff_status["policy"]["minimum_priority"], 5)
        self.assertTrue(
            self.client.automatic_detection(self.session.id)["policy"]["enabled"]
        )

        disabled_detection = self.client.configure_automatic_detection(
            self.session.id, {"enabled": False}
        )
        self.assertFalse(disabled_detection["policy"]["enabled"])
        self.assertEqual(disabled_detection["policy"]["threshold_dbm"], -65.0)
        self.assertTrue(
            self.client.automatic_handoff(self.session.id)["policy"]["enabled"]
        )
        disabled_handoff = self.client.configure_automatic_handoff(
            self.session.id, {"enabled": False}
        )
        self.assertFalse(disabled_handoff["policy"]["enabled"])
        self.assertEqual(disabled_handoff["policy"]["receiver_stream_id"], self.receiver_id)

        completion = automatic_completion_payload(
            enabled=True,
            interval_seconds=0.2,
            minimum_listening_seconds=0,
            minimum_observations=1,
            receiver_stream_id=self.receiver_id,
        )
        completion_status = self.client.configure_automatic_completion(
            self.session.id, completion
        )
        self.assertTrue(completion_status["policy"]["enabled"])
        self.assertEqual(completion_status["policy"]["minimum_observations"], 1)
        snapshot = self.client.snapshot(self.session.id)
        self.assertTrue(snapshot.automatic_completion["policy"]["enabled"])
        self.assertEqual(snapshot.automatic_completion["policy"]["receiver_stream_id"], self.receiver_id)
        disabled_completion = self.client.configure_automatic_completion(
            self.session.id, {"enabled": False}
        )
        self.assertFalse(disabled_completion["policy"]["enabled"])
        self.assertEqual(disabled_completion["policy"]["minimum_observations"], 1)
        self.assertFalse(self.client.automatic_detection(self.session.id)["policy"]["enabled"])
        self.assertFalse(self.client.automatic_handoff(self.session.id)["policy"]["enabled"])
        self.client.stop_session(self.session.id)

    def test_candidate_queue_confirmed_handoff_and_real_completion(self) -> None:
        self.client.start_session(self.session.id)
        self.client.start_workers(self.session.id)
        candidate = self.client.create_candidate(
            self.session.id,
            {
                "id": "candidate/windows",
                "source_stream_id": self.tinysa_id,
                "interval": measurement_interval(1),
                "center_frequency_hz": 102_500_000,
                "bandwidth_hz": 100_000,
                "peak_level": -42.5,
                "level_unit": "dBm",
                "observation_ids": [],
            },
        )
        self.assertEqual(candidate["state"], "new")
        investigation = self.client.enqueue_investigation(
            self.session.id,
            candidate["id"],
            {
                "id": "investigation/windows",
                "target_stream_id": self.receiver_id,
                "target_frequency_hz": 102_500_000,
                "mode": "DMR",
                "priority": 7,
            },
        )
        self.assertEqual(investigation["state"], "queued")
        self.assertEqual(
            self.client.next_investigation(self.session.id)["id"],
            "investigation/windows",
        )
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            self.client.start_next_investigation(
                self.session.id, investigation["id"], confirm=False
            )
        started = self.client.start_next_investigation(
            self.session.id, investigation["id"], confirm=True
        )
        self.assertEqual(started["state"], "listening")
        wait_until(
            lambda: any(
                item.frequency_hz == 102_500_000
                for item in self.server.session_service.list_observations(self.session.id)
            ),
            "Receiver должен записать наблюдения на частоте подтверждённого handoff",
        )
        completed = self.client.complete_investigation(
            self.session.id, investigation["id"]
        )
        self.assertEqual(completed["state"], "completed")
        self.assertTrue(completed["result_observation_ids"])
        candidates = self.client.list_candidates(self.session.id)
        self.assertEqual(candidates[0]["state"], "resolved")
        self.assertEqual(candidates[0]["level_unit"], "dBm")
        self.client.stop_session(self.session.id)

    def test_manual_start_cancel_fail_and_dismiss_use_encoded_ids(self) -> None:
        self.client.start_session(self.session.id)
        self.client.start_workers(self.session.id)

        def create(candidate_id):
            return self.client.create_candidate(
                self.session.id,
                {
                    "id": candidate_id,
                    "source_stream_id": self.tinysa_id,
                    "interval": measurement_interval(1),
                    "center_frequency_hz": 102_500_000,
                    "level_unit": "dBm",
                    "observation_ids": [],
                },
            )

        first = create("candidate/cancel")
        queued = self.client.enqueue_investigation(
            self.session.id,
            first["id"],
            {"id": "investigation/cancel", "target_stream_id": self.receiver_id},
        )
        started = self.client.start_investigation(self.session.id, queued["id"])
        self.assertEqual(started["state"], "listening")
        cancelled = self.client.cancel_investigation(self.session.id, queued["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        dismissed = self.client.dismiss_candidate(self.session.id, first["id"])
        self.assertEqual(dismissed["state"], "dismissed")

        second = create("candidate/fail")
        failed_queue = self.client.enqueue_investigation(
            self.session.id,
            second["id"],
            {"id": "investigation/fail", "target_stream_id": self.receiver_id},
        )
        with self.assertRaisesRegex(ValueError, "не должна быть пустой"):
            self.client.fail_investigation(
                self.session.id, failed_queue["id"], error=" "
            )
        failed = self.client.fail_investigation(
            self.session.id, failed_queue["id"], error="Проверка Windows GUI"
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"], "Проверка Windows GUI")
        self.client.stop_session(self.session.id)

    def test_invalid_tune_is_rejected_before_http(self) -> None:
        with self.assertRaisesRegex(ValueError, "целым числом Гц"):
            self.client.tune(
                self.session.id,
                self.receiver_id,
                frequency_hz=100.5,
                mode="FM",
            )
        self.assertEqual(self.client.get_session(self.session.id)["state"], "draft")


if __name__ == "__main__":
    unittest.main(verbosity=2)
