"""Автономный handoff только после явной политики оператора."""

from __future__ import annotations

import threading
import unittest

from radioinspector.automatic_handoff import AutomaticHandoffScheduler
from radioinspector.contracts import InvestigationState
from radioinspector.session_service import SessionService
from tests.test_device_workers import simulated_session_payload, wait_until
from tests.test_investigations import candidate_payload


class AutomaticHandoffSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService(history_limit=5000)
        self.session = self.service.create_session(simulated_session_payload())
        self.service.start_session(self.session.id)
        self.spectrum_id, self.receiver_id = [item.id for item in self.session.streams]
        self.tune_calls = []
        self.scheduler = AutomaticHandoffScheduler(
            self.service,
            tune=self.tuned,
            start_background=False,
        )

    def tearDown(self) -> None:
        self.scheduler.close()
        self.service.close()

    def tuned(self, session_id, stream_id, payload):
        self.tune_calls.append((session_id, stream_id, payload))
        return {"session_id": session_id, "stream_id": stream_id, **payload}

    def enqueue(self, investigation_id, candidate_id, priority=0, receiver_id=None):
        self.service.create_candidate(
            self.session.id, candidate_payload(self.spectrum_id, candidate_id)
        )
        return self.service.enqueue_investigation(
            self.session.id,
            candidate_id,
            {
                "id": investigation_id,
                "target_stream_id": receiver_id or self.receiver_id,
                "priority": priority,
                "mode": "DMR",
            },
        )

    def enable(self, **policy):
        return self.scheduler.configure(
            self.session.id, {"enabled": True, **policy}
        )

    def test_default_disabled_and_runtime_policy_requires_new_opt_in(self) -> None:
        investigation = self.enqueue("automatic-off", "candidate-off", priority=10)
        status = self.scheduler.status(self.session.id)
        self.assertFalse(status["policy"]["enabled"])
        self.assertFalse(status["policy_persistent"])
        self.assertTrue(status["restart_requires_opt_in"])
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.assertEqual(self.tune_calls, [])
        self.assertEqual(
            self.service.get_investigation(self.session.id, investigation.id).state,
            InvestigationState.QUEUED,
        )

        replacement = AutomaticHandoffScheduler(
            self.service, tune=self.tuned, start_background=False
        )
        self.assertFalse(replacement.status(self.session.id)["policy"]["enabled"])
        replacement.close()

    def test_explicit_policy_priority_filter_and_busy_receiver(self) -> None:
        low = self.enqueue("automatic-low", "candidate-low", priority=2)
        high = self.enqueue("automatic-high", "candidate-high", priority=8)
        self.enable(minimum_priority=5)
        result = self.scheduler.run_once(self.session.id)
        self.assertEqual(result.id, high.id)
        self.assertEqual(result.state, InvestigationState.LISTENING)
        self.assertEqual(len(self.tune_calls), 1)

        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.assertEqual(len(self.tune_calls), 1)
        self.assertEqual(
            self.service.get_investigation(self.session.id, low.id).state,
            InvestigationState.QUEUED,
        )
        self.service.cancel_investigation(self.session.id, high.id)
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.assertEqual(len(self.tune_calls), 1)

        self.scheduler.configure(
            self.session.id, {"enabled": True, "minimum_priority": 0}
        )
        following = self.scheduler.run_once(self.session.id)
        self.assertEqual(following.id, low.id)
        self.assertEqual(len(self.tune_calls), 2)
        status = self.scheduler.status(self.session.id)
        self.assertEqual(status["runtime"]["successful_dispatches"], 2)
        self.assertEqual(status["runtime"]["consecutive_failures"], 0)

    def test_busy_receiver_does_not_block_independent_receiver(self) -> None:
        payload = simulated_session_payload()
        second = dict(payload["streams"][1])
        second["id"] = "ardv1-second"
        second["port"] = "sim://ardv1-second"
        payload["streams"].append(second)
        session = self.service.create_session(payload)
        self.service.start_session(session.id)
        spectrum_id, first_receiver, second_receiver = [
            item.id for item in session.streams
        ]
        self.service.create_candidate(
            session.id, candidate_payload(spectrum_id, "candidate-active")
        )
        active = self.service.enqueue_investigation(
            session.id,
            "candidate-active",
            {"id": "active-first", "target_stream_id": first_receiver},
        )
        self.service.start_investigation(session.id, active.id, tune=self.tuned)
        self.service.create_candidate(
            session.id, candidate_payload(spectrum_id, "candidate-second")
        )
        queued = self.service.enqueue_investigation(
            session.id,
            "candidate-second",
            {
                "id": "queued-second",
                "target_stream_id": second_receiver,
                "priority": 1,
            },
        )
        self.scheduler.configure(session.id, {"enabled": True})
        result = self.scheduler.run_once(session.id)
        self.assertEqual(result.id, queued.id)
        self.assertEqual(result.target_stream_id, second_receiver)
        self.assertEqual(
            self.service.get_investigation(session.id, active.id).state,
            InvestigationState.LISTENING,
        )

    def test_failures_have_backoff_and_open_circuit_breaker(self) -> None:
        self.scheduler._tune = self.fail_tune
        first = self.enqueue("automatic-fail-1", "candidate-fail-1", priority=8)
        second = self.enqueue("automatic-fail-2", "candidate-fail-2", priority=4)
        self.enable(max_consecutive_failures=2, retry_delay_seconds=60.0)

        first_result = self.scheduler.run_once(self.session.id)
        self.assertEqual(first_result.id, first.id)
        self.assertEqual(first_result.state, InvestigationState.FAILED)
        status = self.scheduler.status(self.session.id)
        self.assertTrue(status["policy"]["enabled"])
        self.assertEqual(status["runtime"]["consecutive_failures"], 1)
        self.assertGreaterEqual(
            self.scheduler._next_run[self.session.id],
            self.scheduler._monotonic() + 59.0,
        )

        second_result = self.scheduler.run_once(self.session.id)
        self.assertEqual(second_result.id, second.id)
        self.assertEqual(second_result.state, InvestigationState.FAILED)
        status = self.scheduler.status(self.session.id)
        self.assertFalse(status["policy"]["enabled"])
        self.assertEqual(status["runtime"]["last_result"], "circuit_breaker")
        self.assertEqual(status["runtime"]["consecutive_failures"], 2)
        self.assertEqual(
            self.service.events()[-1].event_type, "automatic_handoff.disabled"
        )
        calls = len(self.tune_calls)
        self.assertIsNone(self.scheduler.run_once(self.session.id))
        self.assertEqual(len(self.tune_calls), calls)

    def fail_tune(self, session_id, stream_id, payload):
        self.tune_calls.append((session_id, stream_id, payload))
        raise RuntimeError("Стенд: receiver недоступен")

    def test_policy_validation_and_explicit_disable(self) -> None:
        with self.assertRaisesRegex(ValueError, "только receiver"):
            self.scheduler.configure(
                self.session.id,
                {"enabled": True, "receiver_stream_id": self.spectrum_id},
            )
        with self.assertRaisesRegex(ValueError, "JSON boolean"):
            self.scheduler.configure(self.session.id, {"enabled": 1})
        with self.assertRaisesRegex(ValueError, "Неизвестные поля"):
            self.scheduler.configure(self.session.id, {"enable": True})
        self.assertFalse(self.scheduler.status(self.session.id)["policy"]["enabled"])

        self.enable(receiver_stream_id=self.receiver_id)
        disabled = self.scheduler.configure(self.session.id, {"enabled": False})
        self.assertFalse(disabled["policy"]["enabled"])
        self.assertEqual(
            disabled["policy"]["receiver_stream_id"], self.receiver_id
        )
        self.assertEqual(
            self.service.events()[-1].event_type,
            "automatic_handoff.policy_changed",
        )


class AutomaticHandoffBackgroundTests(unittest.TestCase):
    def test_background_dispatches_only_after_enable_and_stops_after_disable(self) -> None:
        service = SessionService(history_limit=5000)
        scheduler = AutomaticHandoffScheduler(
            service,
            tune=lambda session_id, stream_id, payload: {
                "session_id": session_id,
                "stream_id": stream_id,
                **payload,
            },
        )
        try:
            session = service.create_session(simulated_session_payload())
            service.start_session(session.id)
            spectrum_id, receiver_id = [item.id for item in session.streams]
            service.create_candidate(
                session.id, candidate_payload(spectrum_id, "candidate-background")
            )
            investigation = service.enqueue_investigation(
                session.id,
                "candidate-background",
                {"id": "background-investigation", "target_stream_id": receiver_id},
            )
            wait_until(
                lambda: service.get_investigation(
                    session.id, investigation.id
                ).state is InvestigationState.QUEUED,
                "Без opt-in исследование должно оставаться queued",
            )
            scheduler.configure(
                session.id, {"enabled": True, "interval_seconds": 0.1}
            )
            wait_until(
                lambda: service.get_investigation(
                    session.id, investigation.id
                ).state is InvestigationState.LISTENING,
                "После opt-in планировщик должен выполнить handoff",
            )
            scheduler.configure(session.id, {"enabled": False})
            self.assertFalse(scheduler.status(session.id)["policy"]["enabled"])
        finally:
            scheduler.close()
            service.close()

    def test_blocked_tune_does_not_serialize_another_session(self) -> None:
        service = SessionService(history_limit=5000)
        entered = threading.Event()
        release = threading.Event()

        def tune(session_id, stream_id, payload):
            if session_id == first.id:
                entered.set()
                if not release.wait(timeout=4):
                    raise RuntimeError("Стенд: не дождался разблокировки tune")
            return {"session_id": session_id, "stream_id": stream_id, **payload}

        scheduler = AutomaticHandoffScheduler(service, tune=tune)
        first = second = None
        try:
            first = service.create_session(simulated_session_payload())
            second = service.create_session(simulated_session_payload())
            investigations = {}
            for session, label in ((first, "first"), (second, "second")):
                service.start_session(session.id)
                spectrum_id, receiver_id = [item.id for item in session.streams]
                service.create_candidate(
                    session.id,
                    candidate_payload(spectrum_id, f"candidate-{label}"),
                )
                investigations[session.id] = service.enqueue_investigation(
                    session.id,
                    f"candidate-{label}",
                    {
                        "id": f"investigation-{label}",
                        "target_stream_id": receiver_id,
                    },
                )
            scheduler.configure(first.id, {"enabled": True, "interval_seconds": 0.1})
            if not entered.wait(timeout=2):
                self.fail("Первый сеанс не начал заблокированную перестройку")
            scheduler.configure(second.id, {"enabled": True, "interval_seconds": 0.1})
            try:
                wait_until(
                    lambda: service.get_investigation(
                        second.id, investigations[second.id].id
                    ).state is InvestigationState.LISTENING,
                    "Медленный tune первого сеанса не должен задерживать второй",
                    timeout=1.5,
                )
            finally:
                release.set()
            wait_until(
                lambda: service.get_investigation(
                    first.id, investigations[first.id].id
                ).state is InvestigationState.LISTENING,
                "Первый сеанс должен завершить handoff после разблокировки",
            )
        finally:
            release.set()
            scheduler.close()
            service.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
