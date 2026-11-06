"""Автоматическое обнаружение кандидатов только после явной политики."""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from datetime import timedelta

from radioinspector.automatic_detection import AutomaticCandidateDetector
from radioinspector.contracts import TimeInterval, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.web.server import create_server
from tests.test_automatic_handoff import AutomaticHandoffScheduler
from tests.test_device_workers import simulated_session_payload, wait_until
from tests.test_investigations import candidate_payload
from tests.test_persistence import sweep_payload
from tests.test_platform import BASE


class AutomaticDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService(history_limit=5000)
        self.session = self.service.create_session(simulated_session_payload())
        self.service.start_session(self.session.id)
        self.spectrum_id, self.receiver_id = [item.id for item in self.session.streams]
        self.detector = AutomaticCandidateDetector(self.service, start_background=False)

    def tearDown(self) -> None:
        self.detector.close()
        self.service.close()

    def test_default_disabled_and_restart_requires_opt_in(self) -> None:
        self.service.ingest_sweep(sweep_payload(self.session.id, self.spectrum_id))
        status = self.detector.status(self.session.id)
        self.assertFalse(status["policy"]["enabled"])
        self.assertFalse(status["policy_persistent"])
        self.assertTrue(status["restart_requires_opt_in"])
        self.assertTrue(status["processes_existing_archive"]) is False
        self.assertFalse(status["automatically_enqueues_investigations"])
        self.assertIsNone(self.detector.run_once(self.session.id))
        self.assertEqual(self.service.list_candidates(self.session.id), [])
        replacement = AutomaticCandidateDetector(self.service, start_background=False)
        self.assertFalse(replacement.status(self.session.id)["policy"]["enabled"])
        replacement.close()

    def test_explicit_detection_creates_candidates_without_handoff(self) -> None:
        self.detector.configure(
            self.session.id,
            {
                "enabled": True,
                "stream_id": self.spectrum_id,
                "threshold_dbm": -60.0,
                "dedup_bandwidth_hz": 100_000,
                "cooldown_seconds": 0.0,
            },
        )
        first = self.service.ingest_sweep(
            sweep_payload(self.session.id, self.spectrum_id, 1)
        )
        result = self.detector.run_once(self.session.id)
        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual(candidate.center_frequency_hz, 100_100_001)
        self.assertEqual(candidate.peak_level, -42.0)
        self.assertEqual(candidate.level_unit, "dBm")
        self.assertEqual(candidate.state.value, "new")
        self.assertEqual(candidate.observation_ids, (first.id,))
        self.assertEqual(self.service.list_investigations(self.session.id), [])
        self.assertEqual(
            self.service.events()[-1].event_type,
            "automatic_detection.candidate_created",
        )

    def test_old_archive_is_not_processed_and_duplicates_are_suppressed(self) -> None:
        self.service.ingest_sweep(sweep_payload(self.session.id, self.spectrum_id, 1))
        self.detector.configure(
            self.session.id,
            {
                "enabled": True,
                "stream_id": self.spectrum_id,
                "threshold_dbm": -60.0,
                "dedup_bandwidth_hz": 100_000,
                "cooldown_seconds": 60.0,
            },
        )
        self.assertEqual(self.detector.run_once(self.session.id), [])
        self.service.ingest_sweep(sweep_payload(self.session.id, self.spectrum_id, 2))
        self.assertEqual(len(self.detector.run_once(self.session.id)), 1)
        self.service.ingest_sweep(sweep_payload(self.session.id, self.spectrum_id, 3))
        self.assertEqual(self.detector.run_once(self.session.id), [])
        self.assertEqual(len(self.service.list_candidates(self.session.id)), 1)

    def test_multiple_segments_are_sorted_and_limited(self) -> None:
        payload = sweep_payload(self.session.id, self.spectrum_id, 4)
        payload["frequencies_hz"] = [100_000_000, 100_010_000, 100_020_000, 100_030_000]
        payload["levels"] = [-50.0, -90.0, -40.0, -45.0]
        self.service.ingest_sweep(payload)
        self.detector.configure(
            self.session.id,
            {
                "enabled": True,
                "stream_id": self.spectrum_id,
                "threshold_dbm": -60.0,
                "max_candidates_per_sweep": 1,
            },
        )
        self.detector.run_once(self.session.id)
        candidates = self.service.list_candidates(self.session.id)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].center_frequency_hz, 100_020_000)
        self.assertEqual(candidates[0].peak_level, -40.0)

    def test_policy_validation_and_explicit_disable(self) -> None:
        with self.assertRaisesRegex(ValueError, "spectrum_analyzer"):
            self.detector.configure(
                self.session.id,
                {"enabled": True, "stream_id": self.receiver_id},
            )
        with self.assertRaisesRegex(ValueError, "JSON boolean"):
            self.detector.configure(self.session.id, {"enabled": 1})
        with self.assertRaisesRegex(ValueError, "Неизвестные поля"):
            self.detector.configure(self.session.id, {"enable": True})
        with self.assertRaisesRegex(ValueError, "threshold_dbm"):
            self.detector.configure(
                self.session.id, {"enabled": True, "threshold_dbm": float("nan")}
            )
        self.detector.configure(
            self.session.id,
            {"enabled": True, "threshold_dbm": -70.0, "cooldown_seconds": 10.0},
        )
        disabled = self.detector.configure(self.session.id, {"enabled": False})
        self.assertFalse(disabled["policy"]["enabled"])
        self.assertEqual(disabled["policy"]["threshold_dbm"], -70.0)
        self.assertEqual(disabled["policy"]["cooldown_seconds"], 10.0)


class AutomaticDetectionBackgroundTests(unittest.TestCase):
    def test_background_waits_for_opt_in_and_stops_after_disable(self) -> None:
        service = SessionService(history_limit=5000)
        detector = AutomaticCandidateDetector(service)
        try:
            session = service.create_session(simulated_session_payload())
            service.start_session(session.id)
            stream_id = session.streams[0].id
            service.ingest_sweep(sweep_payload(session.id, stream_id, "background-off"))
            detector.configure(
                session.id,
                {"enabled": True, "interval_seconds": 0.1, "threshold_dbm": -60.0},
            )
            service.ingest_sweep(sweep_payload(session.id, stream_id, "background-on", 2))
            wait_until(
                lambda: len(service.list_candidates(session.id)) == 1,
                "После opt-in новый sweep должен дать кандидата",
            )
            detector.configure(session.id, {"enabled": False})
            count = len(service.list_candidates(session.id))
            service.ingest_sweep(sweep_payload(session.id, stream_id, "background-off-again", 3))
            self.assertEqual(len(service.list_candidates(session.id)), count)
        finally:
            detector.close()
            service.close()

    def test_blocked_archive_read_does_not_serialize_another_session(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingService(SessionService):
            def list_sweeps(self, session_id, **kwargs):
                if session_id == first.id:
                    entered.set()
                    if not release.wait(timeout=4):
                        raise TimeoutError("Стенд: не разблокировано чтение архива")
                return super().list_sweeps(session_id, **kwargs)

        service = BlockingService(history_limit=5000)
        detector = AutomaticCandidateDetector(service)
        first = second = None
        try:
            first = service.create_session(simulated_session_payload())
            second = service.create_session(simulated_session_payload())
            for session, label in ((first, "first"), (second, "second")):
                service.start_session(session.id)
                stream_id = session.streams[0].id
                service.ingest_sweep(sweep_payload(session.id, stream_id, label))
            detector.configure(first.id, {"enabled": True, "interval_seconds": 0.1})
            if not entered.wait(timeout=2):
                self.fail("Первый сеанс не начал заблокированное чтение")
            detector.configure(second.id, {"enabled": True, "interval_seconds": 0.1})
            try:
                wait_until(
                    lambda: len(service.list_candidates(second.id)) == 1,
                    "Медленное чтение первого сеанса не должно блокировать второй",
                    timeout=1.5,
                )
            finally:
                release.set()
            wait_until(
                lambda: len(service.list_candidates(first.id)) == 1,
                "Первый сеанс должен обнаружить кандидата после разблокировки",
            )
        finally:
            release.set()
            detector.close()
            service.close()


class AutomaticDetectionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService(history_limit=5000)
        self.server = create_server(host="127.0.0.1", port=0, sessions=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
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

    def test_http_policy_round_trip_and_new_sweep_detection(self) -> None:
        session = self.service.create_session(simulated_session_payload())
        self.service.start_session(session.id)
        stream_id = session.streams[0].id
        path = f"/api/v1/sessions/{session.id}/automatic-detection"
        status, initial = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertFalse(initial["data"]["policy"]["enabled"])
        status, configured = self.request(
            "POST", path,
            {
                "enabled": True,
                "interval_seconds": 0.1,
                "stream_id": stream_id,
                "threshold_dbm": -60.0,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(configured["data"]["policy"]["enabled"])
        self.service.ingest_sweep(sweep_payload(session.id, stream_id, "http-new"))
        wait_until(
            lambda: len(self.server.session_service.list_candidates(session.id)) == 2,
            "HTTP opt-in должен запустить обнаружение нового sweep",
        )
        status, runtime = self.request("GET", path)
        self.assertEqual(runtime["data"]["runtime"]["candidates_created"], 2)
        status, disabled = self.request("POST", path, {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(disabled["data"]["policy"]["enabled"])
        status, capabilities = self.request("GET", "/api/v1/capabilities")
        features = capabilities["data"]["features"]
        self.assertTrue(features["automatic_candidate_detection"])
        self.assertTrue(features["automatic_candidate_detection_opt_in_required"])
        self.assertFalse(features["automatic_candidate_detection_policy_persistent"])
        self.assertTrue(features["automatic_investigation_completion"])
        self.assertTrue(features["automatic_investigation_completion_opt_in_required"])
        self.assertFalse(features["automatic_investigation_completion_policy_persistent"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
