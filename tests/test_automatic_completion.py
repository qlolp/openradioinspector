"""Opt-in автоматическое завершение исследований после реальных наблюдений."""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from datetime import timedelta

from radioinspector.automatic_completion import AutomaticInvestigationCompletion
from radioinspector.contracts import InvestigationState, TimeInterval, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.web.server import create_server
from tests.test_device_workers import simulated_session_payload, wait_until
from tests.test_investigations import candidate_payload
from tests.test_persistence import observation_payload
from tests.test_platform import BASE


class CompletionSetup:
    def make_service(self):
        self.now = BASE
        return SessionService(clock=lambda: self.now, history_limit=5000)

    def setup_scheduler(self, *, start_background=False):
        self.service = self.make_service()
        self.session = self.service.create_session(simulated_session_payload())
        self.service.start_session(self.session.id, at=self.now)
        self.spectrum_id, self.receiver_id = [item.id for item in self.session.streams]
        self.scheduler = AutomaticInvestigationCompletion(
            self.service,
            clock=lambda: self.now,
            start_background=start_background,
        )

    def close(self):
        self.scheduler.close()
        self.service.close()

    def tuned(self, session_id, stream_id, payload):
        return {"session_id": session_id, "stream_id": stream_id, **payload}

    def active(self, label="active"):
        self.service.create_candidate(
            self.session.id, candidate_payload(self.spectrum_id, f"candidate-{label}")
        )
        investigation = self.service.enqueue_investigation(
            self.session.id,
            f"candidate-{label}",
            {"id": f"investigation-{label}", "target_stream_id": self.receiver_id},
        )
        return self.service.start_investigation(
            self.session.id, investigation.id, tune=self.tuned
        )

    def observations(self, investigation, count=1, *, frequency_hz=None):
        existing = len(
            self.service.matching_observation_ids(self.session.id, investigation.id)
        )
        for number in range(existing, existing + count):
            started = investigation.started_at + timedelta(seconds=number + 1)
            payload = observation_payload(
                self.session.id, self.receiver_id, number
            )
            payload["id"] = f"{investigation.id}-observation-{number}"
            payload["frequency_hz"] = (
                frequency_hz or investigation.target_frequency_hz
            )
            payload["interval"] = to_primitive(
                TimeInterval(started, started + timedelta(milliseconds=500))
            )
            self.service.ingest_observation(payload)


class AutomaticCompletionTests(CompletionSetup, unittest.TestCase):
    def setUp(self):
        self.setup_scheduler()

    def tearDown(self):
        self.close()

    def test_default_disabled(self):
        investigation = self.active("default-off")
        self.observations(investigation, 2)
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        current = self.service.get_investigation(self.session.id, investigation.id)
        self.assertEqual(current.state, InvestigationState.LISTENING)
        status = self.scheduler.status(self.session.id)
        self.assertFalse(status["policy"]["enabled"])
        self.assertFalse(status["policy_persistent"])
        self.assertTrue(status["restart_requires_opt_in"])
        self.assertFalse(status["automatic_fail_or_cancel"])
        replacement = AutomaticInvestigationCompletion(
            self.service, clock=lambda: self.now, start_background=False
        )
        self.assertFalse(replacement.status(self.session.id)["policy"]["enabled"])
        replacement.close()

    def test_completion_waits_for_time_and_observations(self):
        investigation = self.active("thresholds")
        self.scheduler.configure(
            self.session.id,
            {
                "enabled": True,
                "minimum_listening_seconds": 10.0,
                "minimum_observations": 2,
            },
        )
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.observations(investigation, 1)
        self.now += timedelta(seconds=11)
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.observations(investigation, 1)
        result = self.scheduler.run_once(self.session.id)
        self.assertEqual(result.state, InvestigationState.COMPLETED)
        self.assertEqual(len(result.result_observation_ids), 2)
        status = self.scheduler.status(self.session.id)
        self.assertEqual(status["runtime"]["completed_investigations"], 1)
        self.assertEqual(
            self.service.events()[-1].event_type, "automatic_completion.completed"
        )
        candidate = self.service.get_candidate(
            self.session.id, investigation.candidate_id
        )
        self.assertEqual(candidate.state.value, "resolved")
        self.assertIsNone(self.scheduler.run_once(self.session.id))

    def test_minimum_time_filter_and_no_fail_or_cancel(self):
        investigation = self.active("minimum-time")
        self.observations(investigation, 2)
        self.scheduler.configure(
            self.session.id,
            {"enabled": True, "minimum_listening_seconds": 10.0},
        )
        self.now += timedelta(seconds=5)
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        current = self.service.get_investigation(self.session.id, investigation.id)
        self.assertEqual(current.state, InvestigationState.LISTENING)
        self.now += timedelta(seconds=5)
        result = self.scheduler.run_once(self.session.id)
        self.assertEqual(result.state, InvestigationState.COMPLETED)

        other = self.active("cancelled")
        self.service.cancel_investigation(self.session.id, other.id)
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.assertEqual(
            self.service.get_investigation(self.session.id, other.id).state,
            InvestigationState.CANCELLED,
        )

    def test_manual_race_is_skipped(self):
        investigation = self.active("race")
        self.observations(investigation, 1)
        self.scheduler.configure(self.session.id, {"enabled": True})
        original = self.service.complete_investigation
        self.service.complete_investigation = lambda *_args: (_ for _ in ()).throw(
            ValueError("Исследование уже завершено оператором")
        )
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        status = self.scheduler.status(self.session.id)
        self.assertEqual(status["runtime"]["last_result"], "skipped")
        self.assertEqual(status["runtime"]["skipped_investigations"], 1)
        self.service.complete_investigation = original
        self.assertEqual(
            self.service.get_investigation(self.session.id, investigation.id).state,
            InvestigationState.LISTENING,
        )

    def test_disable_during_preflight_prevents_completion(self) -> None:
        investigation = self.active("disable-inflight")
        self.observations(investigation, 1)
        self.scheduler.configure(self.session.id, {"enabled": True})
        entered = threading.Event()
        release = threading.Event()
        original = self.service.matching_observation_ids

        def blocking(session_id, investigation_id):
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Preflight не разблокирован")
            return original(session_id, investigation_id)

        self.service.matching_observation_ids = blocking
        result = []
        thread = threading.Thread(target=lambda: result.append(self.scheduler.run_once(self.session.id)))
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        self.scheduler.configure(self.session.id, {"enabled": False})
        release.set()
        thread.join(timeout=4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [None])
        self.assertEqual(
            self.service.get_investigation(self.session.id, investigation.id).state,
            InvestigationState.LISTENING,
        )
        status = self.scheduler.status(self.session.id)
        self.assertEqual(status["runtime"]["last_result"], "policy_changed")
        self.assertEqual(status["runtime"]["completed_investigations"], 0)
        self.assertFalse(
            any(item.event_type == "automatic_completion.completed" for item in self.service.events())
        )

    def test_policy_validation(self):
        with self.assertRaisesRegex(ValueError, "только receiver"):
            self.scheduler.configure(
                self.session.id,
                {"enabled": True, "receiver_stream_id": self.spectrum_id},
            )
        with self.assertRaisesRegex(ValueError, "JSON boolean"):
            self.scheduler.configure(self.session.id, {"enabled": 1})
        with self.assertRaisesRegex(ValueError, "Неизвестные поля"):
            self.scheduler.configure(self.session.id, {"enable": True})
        with self.assertRaisesRegex(ValueError, "minimum_observations"):
            self.scheduler.configure(
                self.session.id, {"enabled": True, "minimum_observations": 0}
            )
        disabled = self.scheduler.configure(self.session.id, {"enabled": False})
        self.assertFalse(disabled["policy"]["enabled"])


class BlockingCompletionService(SessionService):
    def __init__(self, first_id, entered, release, armed):
        super().__init__(history_limit=5000)
        self.first_id = first_id
        self.entered = entered
        self.release = release
        self.armed = armed

    def matching_observation_ids(self, session_id, investigation_id):
        if session_id == self.first_id and self.armed.is_set():
            self.entered.set()
            if not self.release.wait(timeout=4):
                raise TimeoutError("Не разблокирован read-only отбор")
        return super().matching_observation_ids(session_id, investigation_id)


class AutomaticCompletionBackgroundTests(CompletionSetup, unittest.TestCase):
    def test_background_sessions_are_independent(self):
        entered = threading.Event()
        release = threading.Event()
        armed = threading.Event()
        first = second = None
        service = BlockingCompletionService("placeholder", entered, release, armed)
        service.first_id = None
        scheduler = AutomaticInvestigationCompletion(service)
        self.service = service
        self.scheduler = scheduler
        try:
            first = service.create_session(simulated_session_payload())
            service.first_id = first.id
            second = service.create_session(simulated_session_payload())
            self.session = first
            self.now = BASE
            investigations = {}
            for session, label in ((first, "first"), (second, "second")):
                self.session = session
                service.start_session(session.id, at=BASE)
                self.spectrum_id, self.receiver_id = [
                    item.id for item in session.streams
                ]
                investigation = self.active(label)
                self.observations(investigation, 1)
                investigations[session.id] = investigation
            armed.set()
            scheduler.configure(first.id, {"enabled": True, "interval_seconds": 0.1})
            self.assertTrue(entered.wait(timeout=2))
            scheduler.configure(second.id, {"enabled": True, "interval_seconds": 0.1})
            wait_until(
                lambda: service.get_investigation(
                    second.id, investigations[second.id].id
                ).state is InvestigationState.COMPLETED,
                "Блокировка первого сеанса не должна останавливать второй",
                timeout=1.5,
            )
            release.set()
            wait_until(
                lambda: service.get_investigation(
                    first.id, investigations[first.id].id
                ).state is InvestigationState.COMPLETED,
                "Первый сеанс должен завершиться после разблокировки",
            )
        finally:
            release.set()
            scheduler.close()
            service.close()


class AutomaticCompletionHttpTests(CompletionSetup, unittest.TestCase):
    def setUp(self):
        self.now = BASE
        self.service = self.make_service()
        self.server = create_server(host="127.0.0.1", port=0, sessions=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.session = self.service.create_session(simulated_session_payload())
        self.service.start_session(self.session.id, at=self.now)
        self.spectrum_id, self.receiver_id = [item.id for item in self.session.streams]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server.automatic_completion.close()
        self.server.automatic_detection.close()
        self.server.automatic_handoff.close()
        self.server.worker_manager.close()
        self.service.close()

    def request(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw)

    def test_http_policy_and_restart_requires_opt_in(self):
        investigation = self.active("http")
        self.observations(investigation, 1)
        status, initial = self.request(
            "GET", f"/api/v1/sessions/{self.session.id}/automatic-completion"
        )
        self.assertEqual(status, 200)
        self.assertFalse(initial["data"]["policy"]["enabled"])
        status, configured = self.request(
            "POST",
            f"/api/v1/sessions/{self.session.id}/automatic-completion",
            {"enabled": True, "interval_seconds": 0.1, "minimum_observations": 1},
        )
        self.assertEqual(status, 200)
        self.assertTrue(configured["data"]["policy"]["enabled"])
        wait_until(
            lambda: self.service.get_investigation(
                self.session.id, investigation.id
            ).state is InvestigationState.COMPLETED,
            "HTTP opt-in должен завершить исследование",
        )
        status, capabilities = self.request("GET", "/api/v1/capabilities")
        features = capabilities["data"]["features"]
        self.assertTrue(features["automatic_investigation_completion"])
        self.assertTrue(features["automatic_investigation_completion_opt_in_required"])
        self.assertFalse(features["automatic_investigation_completion_policy_persistent"])

        self.server.automatic_completion.close()
        replacement = AutomaticInvestigationCompletion(self.service)
        self.assertFalse(replacement.status(self.session.id)["policy"]["enabled"])
        replacement.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
