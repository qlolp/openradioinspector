"""Кандидаты, ручной handoff и очередь исследований двухприборного сеанса."""

from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from radioinspector.contracts import (
    CandidateState,
    InvestigationState,
    ReceiverObservation,
    TimeInterval,
    to_primitive,
)
from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.session_service import SessionNotFoundError, SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_device_workers import simulated_session_payload, wait_until
from tests.test_persistence import sweep_payload
from tests.test_platform import BASE


class Clock:
    def __init__(self, value: datetime = BASE) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def candidate_payload(stream_id: str, candidate_id: str = "candidate-1") -> dict:
    return {
        "id": candidate_id,
        "source_stream_id": stream_id,
        "interval": {
            "started_at": to_primitive(BASE - timedelta(seconds=2)),
            "ended_at": to_primitive(BASE - timedelta(seconds=1)),
        },
        "center_frequency_hz": 102_500_000,
        "bandwidth_hz": 200_000,
        "peak_level": -42.5,
        "level_unit": "dBm",
        "score": 0.9,
    }


def observation(
    session_id: str,
    stream_id: str,
    observation_id: str,
    started_at: datetime,
    frequency_hz: int = 102_500_000,
) -> ReceiverObservation:
    return ReceiverObservation(
        id=observation_id,
        session_id=session_id,
        stream_id=stream_id,
        interval=TimeInterval(started_at, started_at + timedelta(milliseconds=100)),
        frequency_hz=frequency_hz,
        mode="DMR",
        level=41,
        level_unit="s_meter_count",
        squelch_open=True,
        decoded_mode="DMR",
    )


class InvestigationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.service = SessionService(clock=self.clock, history_limit=5000)
        self.session = self.service.create_session(simulated_session_payload())
        self.service.start_session(self.session.id)
        self.spectrum_id, self.receiver_id = [item.id for item in self.session.streams]

    def tearDown(self) -> None:
        self.service.close()

    def candidate(self, candidate_id: str = "candidate-1"):
        return self.service.create_candidate(
            self.session.id, candidate_payload(self.spectrum_id, candidate_id)
        )

    def enqueue(self, candidate_id: str = "candidate-1", **payload):
        return self.service.enqueue_investigation(
            self.session.id,
            candidate_id,
            {"target_stream_id": self.receiver_id, **payload},
        )

    @staticmethod
    def tuned(session_id: str, stream_id: str, payload: dict) -> dict:
        return {"session_id": session_id, "stream_id": stream_id, **payload}

    def test_candidate_preserves_source_unit_and_validates_ownership(self) -> None:
        candidate = self.candidate()
        self.assertEqual(candidate.state, CandidateState.NEW)
        self.assertEqual(candidate.level_unit, "dBm")
        self.assertEqual(candidate.peak_level, -42.5)
        self.assertFalse(hasattr(candidate, "s_meter_count"))
        self.assertEqual(self.service.list_candidates(self.session.id), [candidate])
        self.assertEqual(
            self.service.events()[-1].event_type, "candidate.created"
        )

        wrong_unit = candidate_payload(self.spectrum_id, "wrong-unit")
        wrong_unit["level_unit"] = "s_meter_count"
        with self.assertRaisesRegex(ValueError, "преобразование"):
            self.service.create_candidate(self.session.id, wrong_unit)
        with self.assertRaisesRegex(ValueError, "не принадлежит"):
            self.service.create_candidate(
                self.session.id, candidate_payload("foreign", "foreign-stream")
            )
        with self.assertRaisesRegex(ValueError, "состояние new"):
            self.service.create_candidate(
                self.session.id,
                {**candidate_payload(self.spectrum_id, "injected"), "state": "resolved"},
            )
        with self.assertRaises(SessionNotFoundError):
            self.service.create_candidate(
                "missing", candidate_payload(self.spectrum_id, "wrong-session")
            )

    def test_enqueue_defaults_priority_order_and_duplicate_prevention(self) -> None:
        for candidate_id in ("candidate-low", "candidate-high", "candidate-mid"):
            self.candidate(candidate_id)
        low = self.enqueue("candidate-low", id="investigation-low", priority=1)
        high = self.enqueue("candidate-high", id="investigation-high", priority=9)
        mid = self.enqueue("candidate-mid", id="investigation-mid", priority=5)
        self.assertEqual(low.target_frequency_hz, 102_500_000)
        self.assertEqual(low.state, InvestigationState.QUEUED)
        self.assertEqual(
            self.service.get_candidate(self.session.id, "candidate-low").state,
            CandidateState.QUEUED,
        )
        self.assertEqual(
            [item.id for item in self.service.list_investigations(self.session.id)],
            [high.id, mid.id, low.id],
        )
        self.assertEqual(
            [item.id for item in self.service.list_investigations(
                self.session.id, state="queued"
            )],
            [high.id, mid.id, low.id],
        )
        with self.assertRaisesRegex(ValueError, "открытое исследование"):
            self.enqueue("candidate-high")
        self.candidate("target-check")
        with self.assertRaisesRegex(ValueError, "receiver"):
            self.service.enqueue_investigation(
                self.session.id,
                "target-check",
                {"target_stream_id": self.spectrum_id},
            )
        with self.assertRaisesRegex(ValueError, "created_at"):
            self.enqueue("candidate-low", created_at=to_primitive(BASE))

    def test_semi_automatic_handoff_requires_current_confirmation(self) -> None:
        self.assertIsNone(self.service.next_investigation(self.session.id))
        for candidate_id in ("candidate-low", "candidate-high", "candidate-mid"):
            self.candidate(candidate_id)
        low = self.enqueue("candidate-low", id="investigation-low", priority=1)
        high = self.enqueue("candidate-high", id="investigation-high", priority=9, mode="DMR")
        mid = self.enqueue("candidate-mid", id="investigation-mid", priority=5)
        self.assertEqual(self.service.next_investigation(self.session.id).id, high.id)
        calls = []

        def tune(session_id: str, stream_id: str, payload: dict) -> dict:
            calls.append((session_id, stream_id, payload))
            return self.tuned(session_id, stream_id, payload)

        with self.assertRaisesRegex(ValueError, "Очередь изменилась"):
            self.service.start_next_investigation(
                self.session.id, low.id, tune=tune
            )
        self.assertEqual(calls, [])
        self.assertEqual(
            self.service.get_investigation(self.session.id, high.id).state,
            InvestigationState.QUEUED,
        )

        started = self.service.start_next_investigation(
            self.session.id, high.id, tune=tune
        )
        self.assertEqual(started.state, InvestigationState.LISTENING)
        self.assertEqual(self.service.next_investigation(self.session.id).id, mid.id)
        with self.assertRaisesRegex(ValueError, "receiver уже выполняет"):
            self.service.start_investigation(self.session.id, mid.id, tune=tune)
        with self.assertRaisesRegex(ValueError, "receiver уже выполняет"):
            self.service.start_next_investigation(self.session.id, mid.id, tune=tune)
        self.assertEqual(len(calls), 1)

        self.service.cancel_investigation(self.session.id, high.id)
        following = self.service.start_next_investigation(
            self.session.id, mid.id, tune=tune
        )
        self.assertEqual(following.state, InvestigationState.LISTENING)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.service.next_investigation(self.session.id).id, low.id)

    def test_start_success_marks_tuning_then_listening(self) -> None:
        self.candidate()
        investigation = self.enqueue(id="investigation-1", mode="DMR")
        calls = []

        def tune(session_id: str, stream_id: str, payload: dict) -> dict:
            calls.append((session_id, stream_id, payload))
            self.assertEqual(
                self.service.get_investigation(self.session.id, investigation.id).state,
                InvestigationState.TUNING,
            )
            return self.tuned(session_id, stream_id, payload)

        started = self.service.start_investigation(
            self.session.id, investigation.id, tune=tune
        )
        self.assertEqual(started.state, InvestigationState.LISTENING)
        self.assertEqual(started.started_at, BASE)
        self.assertIsNone(started.ended_at)
        self.assertEqual(
            calls,
            [(self.session.id, self.receiver_id, {
                "frequency_hz": 102_500_000, "mode": "DMR"
            })],
        )
        self.assertEqual(
            self.service.get_candidate(self.session.id, candidate_id="candidate-1").state,
            CandidateState.INVESTIGATING,
        )
        self.assertEqual(
            [item.event_type for item in self.service.events()][-2:],
            ["investigation.tuning", "investigation.listening"],
        )

    def test_tune_failure_is_retryable_and_does_not_create_result(self) -> None:
        self.candidate()
        investigation = self.enqueue(id="investigation-failed")

        def fail(_session_id: str, _stream_id: str, _payload: dict) -> dict:
            raise RuntimeError("AR-DV1 занят")

        failed = self.service.start_investigation(
            self.session.id, investigation.id, tune=fail
        )
        self.assertEqual(failed.state, InvestigationState.FAILED)
        self.assertEqual(failed.error, "AR-DV1 занят")
        self.assertEqual(failed.result_observation_ids, ())
        self.assertEqual(
            self.service.get_candidate(self.session.id, "candidate-1").state,
            CandidateState.QUEUED,
        )
        retry = self.enqueue(id="investigation-retry")
        listening = self.service.start_investigation(
            self.session.id, retry.id, tune=self.tuned
        )
        self.assertEqual(listening.state, InvestigationState.LISTENING)
        self.assertEqual(len(self.service.list_investigations(self.session.id)), 2)

    def test_complete_links_only_matching_new_observations(self) -> None:
        self.candidate()
        investigation = self.enqueue(id="investigation-complete")
        self.clock.advance(10)
        self.service.start_investigation(
            self.session.id, investigation.id, tune=self.tuned
        )
        with self.assertRaisesRegex(ValueError, "Нет наблюдений"):
            self.service.complete_investigation(self.session.id, investigation.id)
        self.service.ingest_observation(observation(
            self.session.id, self.receiver_id, "old", BASE + timedelta(seconds=9)
        ))
        self.service.ingest_observation(observation(
            self.session.id, self.receiver_id, "wrong-frequency",
            BASE + timedelta(seconds=11), frequency_hz=145_500_000,
        ))
        self.clock.advance(1)
        self.service.ingest_observation(observation(
            self.session.id, self.receiver_id, "match-b", self.clock.value
        ))
        self.service.ingest_observation(observation(
            self.session.id, self.receiver_id, "match-a", self.clock.value
        ))
        self.clock.advance(1)
        completed = self.service.complete_investigation(
            self.session.id, investigation.id
        )
        self.assertEqual(completed.state, InvestigationState.COMPLETED)
        self.assertEqual(completed.result_observation_ids, ("match-a", "match-b"))
        self.assertEqual(
            self.service.get_candidate(self.session.id, "candidate-1").state,
            CandidateState.RESOLVED,
        )
        stored = self.service.list_observations(self.session.id, limit=1000)
        self.assertTrue(all(item.level_unit == "s_meter_count" for item in stored))
        with self.assertRaises(ValueError):
            self.service.complete_investigation(self.session.id, investigation.id)

    def test_fail_cancel_and_dismiss_have_explicit_transitions(self) -> None:
        self.candidate("dismiss-new")
        dismissed = self.service.dismiss_candidate(self.session.id, "dismiss-new")
        self.assertEqual(dismissed.state, CandidateState.DISMISSED)
        with self.assertRaises(ValueError):
            self.enqueue("dismiss-new")

        self.candidate()
        investigation = self.enqueue(id="investigation-cancel")
        with self.assertRaisesRegex(ValueError, "открытое исследование"):
            self.service.dismiss_candidate(self.session.id, "candidate-1")
        cancelled = self.service.cancel_investigation(self.session.id, investigation.id)
        self.assertEqual(cancelled.state, InvestigationState.CANCELLED)
        self.assertIsNotNone(cancelled.ended_at)
        self.service.dismiss_candidate(self.session.id, "candidate-1")

        self.candidate("candidate-fail")
        queued = self.enqueue("candidate-fail", id="investigation-manual-fail")
        failed = self.service.fail_investigation(
            self.session.id, queued.id, "оператор завершил проверку сбоем"
        )
        self.assertEqual(failed.state, InvestigationState.FAILED)
        self.assertEqual(failed.error, "оператор завершил проверку сбоем")
        with self.assertRaises(ValueError):
            self.service.fail_investigation(self.session.id, queued.id, "повтор")

    def test_cancel_during_blocked_tune_is_not_overwritten(self) -> None:
        self.candidate()
        investigation = self.enqueue(id="investigation-race")
        entered = threading.Event()
        release = threading.Event()

        def blocked(session_id: str, stream_id: str, payload: dict) -> dict:
            entered.set()
            self.assertTrue(release.wait(3))
            return self.tuned(session_id, stream_id, payload)

        result = []
        thread = threading.Thread(target=lambda: result.append(
            self.service.start_investigation(
                self.session.id, investigation.id, tune=blocked
            )
        ))
        thread.start()
        self.assertTrue(entered.wait(2))
        cancelled = self.service.cancel_investigation(self.session.id, investigation.id)
        self.assertEqual(cancelled.state, InvestigationState.CANCELLED)
        release.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0].state, InvestigationState.CANCELLED)
        self.assertEqual(
            self.service.get_candidate(self.session.id, "candidate-1").state,
            CandidateState.QUEUED,
        )

    def test_stopping_session_cancels_open_investigations(self) -> None:
        self.candidate("listening-candidate")
        listening = self.enqueue("listening-candidate", id="listening-investigation")
        self.service.start_investigation(
            self.session.id, listening.id, tune=self.tuned
        )
        self.candidate("queued-candidate")
        queued = self.enqueue("queued-candidate", id="queued-investigation")
        stopped = self.service.stop_session(self.session.id)
        self.assertEqual(stopped.state.value, "stopped")
        self.assertEqual(
            self.service.get_investigation(self.session.id, listening.id).state,
            InvestigationState.CANCELLED,
        )
        self.assertEqual(
            self.service.get_investigation(self.session.id, queued.id).state,
            InvestigationState.CANCELLED,
        )
        self.assertEqual(
            self.service.get_candidate(self.session.id, "listening-candidate").state,
            CandidateState.QUEUED,
        )
        self.assertEqual(
            self.service.get_candidate(self.session.id, "queued-candidate").state,
            CandidateState.QUEUED,
        )

    def test_capabilities_distinguish_manual_and_automatic_handoff(self) -> None:
        features = self.service.capabilities()["features"]
        self.assertTrue(features["candidate_investigations"])
        self.assertTrue(features["manual_investigation_handoff"])
        self.assertTrue(features["semi_automatic_handoff"])
        self.assertTrue(features["automatic_handoff"])
        self.assertTrue(features["automatic_handoff_opt_in_required"])
        self.assertFalse(features["automatic_handoff_policy_persistent"])
        self.assertTrue(features["automatic_candidate_detection"])
        self.assertTrue(features["automatic_candidate_detection_opt_in_required"])
        self.assertFalse(features["automatic_candidate_detection_policy_persistent"])
        self.assertFalse(features["automatic_investigation_completion"])
        self.assertFalse(features["hardware_control"])


class WorkerHandoffTests(unittest.TestCase):
    def test_tinysa_continues_while_ardv1_tune_command_is_blocked(self) -> None:
        service = SessionService(history_limit=5000)
        manager = DeviceWorkerManager(service)
        self.addCleanup(service.close)
        self.addCleanup(manager.close)
        session = service.create_session(simulated_session_payload())
        service.start_session(session.id)
        spectrum_id, receiver_id = [item.id for item in session.streams]
        manager.start_session(session.id)
        wait_until(
            lambda: len(service.list_sweeps(session.id)) >= 3
            and len(service.list_observations(session.id)) >= 3,
            "Оба worker должны начать запись",
        )
        candidate = service.create_candidate(
            session.id, candidate_payload(spectrum_id, "worker-candidate")
        )
        investigation = service.enqueue_investigation(
            session.id, candidate.id,
            {"id": "worker-investigation", "target_stream_id": receiver_id, "mode": "DMR"},
        )
        worker = manager._workers[(session.id, receiver_id)]
        original_execute = worker.adapter.execute
        entered = threading.Event()
        release = threading.Event()

        def blocked_execute(command: str, payload: dict) -> dict:
            if command == "tune":
                entered.set()
                if not release.wait(4):
                    raise RuntimeError("тестовая команда не была освобождена")
            return original_execute(command, payload)

        worker.adapter.execute = blocked_execute
        result = []
        errors = []

        def start() -> None:
            try:
                result.append(service.start_investigation(
                    session.id, investigation.id, tune=manager.tune
                ))
            except Exception as exc:  # pragma: no cover - диагностика фонового потока
                errors.append(exc)

        thread = threading.Thread(target=start)
        thread.start()
        try:
            self.assertTrue(entered.wait(2), "Команда настройки должна попасть в AR-DV1")
            self.assertEqual(
                service.get_investigation(session.id, investigation.id).state,
                InvestigationState.TUNING,
            )
            before = len(service.list_sweeps(session.id, limit=1000))
            time.sleep(0.15)
            after = len(service.list_sweeps(session.id, limit=1000))
            self.assertGreater(after, before, "tinySA не должен ждать общий lock")
            statuses = {item["stream_id"]: item for item in manager.list_workers(session.id)}
            self.assertTrue(statuses[spectrum_id]["alive"])
            self.assertTrue(statuses[receiver_id]["alive"])
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0].state, InvestigationState.LISTENING)
        wait_until(
            lambda: any(
                item.frequency_hz == 102_500_000 and item.decoded_mode == "DMR"
                for item in service.list_observations(session.id, limit=1000)
            ),
            "После handoff должны появиться наблюдения цели",
        )
        completed = service.complete_investigation(session.id, investigation.id)
        self.assertTrue(completed.result_observation_ids)


class PersistentInvestigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = str(Path(self.temporary.name) / "investigations.sqlite")

    def service(self) -> SessionService:
        return SessionService(store=SQLiteSessionStore(self.path), clock=Clock())

    def test_restart_marks_runtime_investigation_failed_and_preserves_queue(self) -> None:
        service = self.service()
        session = service.create_session(simulated_session_payload())
        service.start_session(session.id)
        spectrum_id, receiver_id = [item.id for item in session.streams]
        active_candidate = service.create_candidate(
            session.id, candidate_payload(spectrum_id, "active-candidate")
        )
        active = service.enqueue_investigation(
            session.id, active_candidate.id,
            {"id": "active-investigation", "target_stream_id": receiver_id},
        )
        service.start_investigation(session.id, active.id, tune=InvestigationServiceTests.tuned)
        queued_candidate = service.create_candidate(
            session.id, candidate_payload(spectrum_id, "queued-candidate")
        )
        queued = service.enqueue_investigation(
            session.id, queued_candidate.id,
            {"id": "queued-investigation", "target_stream_id": receiver_id, "priority": 4},
        )
        service.close()

        restored = self.service()
        try:
            interrupted = restored.get_investigation(session.id, active.id)
            self.assertEqual(interrupted.state, InvestigationState.FAILED)
            self.assertIn("перезапуском", interrupted.error)
            self.assertIsNotNone(interrupted.ended_at)
            self.assertEqual(
                restored.get_candidate(session.id, active_candidate.id).state,
                CandidateState.QUEUED,
            )
            self.assertEqual(
                restored.get_investigation(session.id, queued.id).state,
                InvestigationState.QUEUED,
            )
            self.assertEqual(
                restored.get_candidate(session.id, queued_candidate.id).state,
                CandidateState.QUEUED,
            )
            self.assertTrue(all(
                item.state.value == "disconnected"
                for item in restored.get_session(session.id).streams
            ))
            self.assertEqual(restored.events()[-1].event_type, "investigation.interrupted")
            retry = restored.enqueue_investigation(
                session.id, active_candidate.id,
                {"id": "retry-after-restart", "target_stream_id": receiver_id},
            )
            self.assertEqual(retry.state, InvestigationState.QUEUED)
        finally:
            restored.close()

    def test_event_failure_rolls_back_candidate_and_investigation_together(self) -> None:
        service = self.service()
        session = service.create_session(simulated_session_payload())
        spectrum_id, receiver_id = [item.id for item in session.streams]
        candidate = service.create_candidate(
            session.id, candidate_payload(spectrum_id, "atomic-candidate")
        )
        before = service.health()["last_event_id"]
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_queue_event BEFORE INSERT ON events "
                "WHEN NEW.event_type = 'investigation.queued' "
                "BEGIN SELECT RAISE(ABORT, 'injected queue event failure'); END"
            )
        with self.assertRaisesRegex(ValueError, "нарушает связи"):
            service.enqueue_investigation(
                session.id, candidate.id,
                {"id": "atomic-investigation", "target_stream_id": receiver_id},
            )
        self.assertEqual(service.list_investigations(session.id), [])
        self.assertEqual(
            service.get_candidate(session.id, candidate.id).state, CandidateState.NEW
        )
        self.assertEqual(service.health()["last_event_id"], before)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT state FROM candidates WHERE id = 'atomic-candidate'"
            ).fetchone()[0], "new")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM investigations"
            ).fetchone()[0], 0)
            connection.execute("DROP TRIGGER reject_queue_event")
        queued = service.enqueue_investigation(
            session.id, candidate.id,
            {"id": "atomic-investigation", "target_stream_id": receiver_id},
        )
        self.assertEqual(queued.state, InvestigationState.QUEUED)
        self.assertEqual(service.health()["last_event_id"], before + 1)
        service.close()

    def test_completion_event_failure_does_not_publish_false_result(self) -> None:
        service = self.service()
        session = service.create_session(simulated_session_payload())
        service.start_session(session.id)
        spectrum_id, receiver_id = [item.id for item in session.streams]
        candidate = service.create_candidate(session.id, candidate_payload(spectrum_id))
        investigation = service.enqueue_investigation(
            session.id, candidate.id, {"target_stream_id": receiver_id}
        )
        service.start_investigation(
            session.id, investigation.id, tune=InvestigationServiceTests.tuned
        )
        service.ingest_observation(observation(
            session.id, receiver_id, "atomic-result", BASE + timedelta(seconds=1)
        ))
        before = service.health()["last_event_id"]
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_complete_event BEFORE INSERT ON events "
                "WHEN NEW.event_type = 'investigation.completed' "
                "BEGIN SELECT RAISE(ABORT, 'injected complete event failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            service.complete_investigation(session.id, investigation.id)
        current = service.get_investigation(session.id, investigation.id)
        self.assertEqual(current.state, InvestigationState.LISTENING)
        self.assertEqual(current.result_observation_ids, ())
        self.assertEqual(service.health()["last_event_id"], before)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT state, result_observation_ids_json FROM investigations WHERE id = ?",
                (investigation.id,),
            ).fetchone()
            self.assertEqual(row, ("listening", "[]"))
            self.assertEqual(connection.execute(
                "SELECT state FROM candidates WHERE id = ?", (candidate.id,)
            ).fetchone()[0], "investigating")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        service.close()


class InvestigationHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.automatic_detection.close()
        self.server.automatic_handoff.close()
        self.server.worker_manager.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server.session_service.close()

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw.decode("utf-8"))

    def create_session(self) -> tuple[str, str, str]:
        status, created = self.request("POST", "/api/v1/sessions", simulated_session_payload())
        self.assertEqual(status, 201)
        session = created["data"]
        spectrum_id, receiver_id = [item["id"] for item in session["streams"]]
        return session["id"], spectrum_id, receiver_id

    def test_http_manual_handoff_round_trip(self) -> None:
        session_id, spectrum_id, receiver_id = self.create_session()
        self.request("POST", f"/api/v1/sessions/{session_id}/start", {})
        self.request("POST", f"/api/v1/sessions/{session_id}/workers/start", {})
        status, candidate = self.request(
            "POST", f"/api/v1/sessions/{session_id}/candidates",
            candidate_payload(spectrum_id, "http-candidate"),
        )
        self.assertEqual(status, 201)
        self.assertEqual(candidate["data"]["level_unit"], "dBm")
        status, investigation = self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/candidates/http-candidate/investigations",
            {"id": "http-investigation", "target_stream_id": receiver_id, "mode": "DMR"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(investigation["data"]["state"], "queued")
        status, listening = self.request(
            "POST", f"/api/v1/sessions/{session_id}/investigations/http-investigation/start", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(listening["data"]["state"], "listening")
        wait_until(
            lambda: any(
                item.frequency_hz == 102_500_000 and item.decoded_mode == "DMR"
                for item in self.server.session_service.list_observations(
                    session_id, stream_id=receiver_id, limit=1000
                )
            ),
            "HTTP handoff должен настроить только AR-DV1",
        )
        status, completed = self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/investigations/http-investigation/complete",
            {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["data"]["state"], "completed")
        self.assertTrue(completed["data"]["result_observation_ids"])
        status, resolved = self.request(
            "GET", f"/api/v1/sessions/{session_id}/candidates/http-candidate"
        )
        self.assertEqual(status, 200)
        self.assertEqual(resolved["data"]["state"], "resolved")
        status, listing = self.request(
            "GET", f"/api/v1/sessions/{session_id}/investigations?state=completed"
        )
        self.assertEqual([item["id"] for item in listing["data"]], ["http-investigation"])
        status, capabilities = self.request("GET", "/api/v1/capabilities")
        self.assertTrue(capabilities["data"]["features"]["manual_investigation_handoff"])
        self.assertTrue(capabilities["data"]["features"]["automatic_handoff"])

    def test_http_semi_automatic_preview_and_confirmation(self) -> None:
        session_id, spectrum_id, receiver_id = self.create_session()
        self.request("POST", f"/api/v1/sessions/{session_id}/start", {})
        self.request("POST", f"/api/v1/sessions/{session_id}/workers/start", {})
        status, empty = self.request(
            "GET", f"/api/v1/sessions/{session_id}/investigations/next"
        )
        self.assertEqual(status, 200)
        self.assertIsNone(empty["data"])

        for candidate_id in ("http-low", "http-high"):
            status, _ = self.request(
                "POST", f"/api/v1/sessions/{session_id}/candidates",
                candidate_payload(spectrum_id, candidate_id),
            )
            self.assertEqual(status, 201)
        for investigation_id, candidate_id, priority in (
            ("http-investigation-low", "http-low", 1),
            ("http-investigation-high", "http-high", 8),
        ):
            status, _ = self.request(
                "POST",
                f"/api/v1/sessions/{session_id}/candidates/{candidate_id}/investigations",
                {
                    "id": investigation_id,
                    "target_stream_id": receiver_id,
                    "priority": priority,
                    "mode": "DMR",
                },
            )
            self.assertEqual(status, 201)

        status, recommended = self.request(
            "GET", f"/api/v1/sessions/{session_id}/investigations/next"
        )
        self.assertEqual(recommended["data"]["id"], "http-investigation-high")
        status, rejected = self.request(
            "POST", f"/api/v1/sessions/{session_id}/investigations/start-next",
            {"investigation_id": "http-investigation-high", "confirm": False},
        )
        self.assertEqual(status, 400)
        self.assertIn("confirm=true", rejected["error"]["message"])
        status, stale = self.request(
            "POST", f"/api/v1/sessions/{session_id}/investigations/start-next",
            {"investigation_id": "http-investigation-low", "confirm": True},
        )
        self.assertEqual(status, 400)
        self.assertIn("Очередь изменилась", stale["error"]["message"])
        status, current = self.request(
            "GET",
            f"/api/v1/sessions/{session_id}/investigations/http-investigation-high",
        )
        self.assertEqual(current["data"]["state"], "queued")

        status, listening = self.request(
            "POST", f"/api/v1/sessions/{session_id}/investigations/start-next",
            {"investigation_id": "http-investigation-high", "confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(listening["data"]["state"], "listening")
        status, following = self.request(
            "GET", f"/api/v1/sessions/{session_id}/investigations/next"
        )
        self.assertEqual(following["data"]["id"], "http-investigation-low")
        status, capabilities = self.request("GET", "/api/v1/capabilities")
        self.assertTrue(capabilities["data"]["features"]["semi_automatic_handoff"])
        self.assertTrue(capabilities["data"]["features"]["automatic_handoff"])

    def test_http_automatic_handoff_requires_opt_in_and_can_be_disabled(self) -> None:
        session_id, spectrum_id, receiver_id = self.create_session()
        self.request("POST", f"/api/v1/sessions/{session_id}/start", {})
        self.request("POST", f"/api/v1/sessions/{session_id}/workers/start", {})
        status, _ = self.request(
            "POST", f"/api/v1/sessions/{session_id}/candidates",
            candidate_payload(spectrum_id, "http-automatic-candidate"),
        )
        self.assertEqual(status, 201)
        status, _ = self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/candidates/http-automatic-candidate/investigations",
            {
                "id": "http-automatic-investigation",
                "target_stream_id": receiver_id,
                "mode": "DMR",
            },
        )
        self.assertEqual(status, 201)
        policy_path = f"/api/v1/sessions/{session_id}/automatic-handoff"
        status, initial = self.request("GET", policy_path)
        self.assertEqual(status, 200)
        self.assertFalse(initial["data"]["policy"]["enabled"])
        self.assertFalse(initial["data"]["policy_persistent"])
        status, rejected = self.request("POST", policy_path, {"enabled": 1})
        self.assertEqual(status, 400)
        status, enabled = self.request(
            "POST", policy_path,
            {
                "enabled": True,
                "interval_seconds": 0.1,
                "receiver_stream_id": receiver_id,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(enabled["data"]["policy"]["enabled"])
        wait_until(
            lambda: self.server.session_service.get_investigation(
                session_id, "http-automatic-investigation"
            ).state is InvestigationState.LISTENING,
            "HTTP opt-in должен запустить автоматический handoff",
        )
        status, runtime = self.request("GET", policy_path)
        self.assertEqual(
            runtime["data"]["runtime"]["last_investigation_id"],
            "http-automatic-investigation",
        )
        self.assertEqual(runtime["data"]["runtime"]["successful_dispatches"], 1)
        status, disabled = self.request("POST", policy_path, {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(disabled["data"]["policy"]["enabled"])
        status, capabilities = self.request("GET", "/api/v1/capabilities")
        features = capabilities["data"]["features"]
        self.assertTrue(features["automatic_handoff"])
        self.assertTrue(features["automatic_handoff_opt_in_required"])
        self.assertFalse(features["automatic_handoff_policy_persistent"])
        self.assertTrue(features["automatic_candidate_detection"])

    def test_http_encoded_ids_dismiss_fail_and_not_found(self) -> None:
        session_id, spectrum_id, receiver_id = self.create_session()
        candidate_id = "кандидат/📻"
        investigation_id = "исследование/📻"
        candidate_path = quote(candidate_id, safe="")
        investigation_path = quote(investigation_id, safe="")
        status, _ = self.request(
            "POST", f"/api/v1/sessions/{session_id}/candidates",
            candidate_payload(spectrum_id, candidate_id),
        )
        self.assertEqual(status, 201)
        status, queued = self.request(
            "POST", f"/api/v1/sessions/{session_id}/candidates/{candidate_path}/investigations",
            {"id": investigation_id, "target_stream_id": receiver_id},
        )
        self.assertEqual(status, 201)
        self.assertEqual(queued["data"]["candidate_id"], candidate_id)
        status, fetched = self.request(
            "GET", f"/api/v1/sessions/{session_id}/investigations/{investigation_path}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(fetched["data"]["id"], investigation_id)
        status, error = self.request(
            "POST", f"/api/v1/sessions/{session_id}/investigations/{investigation_path}/fail", {}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", error["error"]["message"])
        status, failed = self.request(
            "POST", f"/api/v1/sessions/{session_id}/investigations/{investigation_path}/fail",
            {"error": "проверка отменена оператором"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(failed["data"]["state"], "failed")
        status, dismissed = self.request(
            "POST", f"/api/v1/sessions/{session_id}/candidates/{candidate_path}/dismiss", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(dismissed["data"]["state"], "dismissed")
        status, missing = self.request(
            "GET", f"/api/v1/sessions/{session_id}/investigations/missing"
        )
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"]["code"], "investigation_not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
