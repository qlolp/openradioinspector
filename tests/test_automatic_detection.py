"""Автоматическое обнаружение кандидатов только после явного opt-in."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from radioinspector.automatic_detection import (
    AutomaticCandidateDetector,
    detect_regions,
)
from radioinspector.contracts import CandidateState, Sweep
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_device_workers import simulated_session_payload, wait_until
from tests.test_persistence import measurement_interval


def spectral_sweep(session_id: str, stream_id: str, sweep_id: str, number: int = 1) -> dict:
    return {
        "id": sweep_id,
        "session_id": session_id,
        "stream_id": stream_id,
        "interval": measurement_interval(number),
        "frequencies_hz": [
            100_000_000,
            100_100_000,
            100_200_000,
            100_300_000,
            100_400_000,
            100_500_000,
            100_600_000,
        ],
        "levels": [-90.0, -55.0, -45.0, -80.0, -50.0, -48.0, -90.0],
        "level_unit": "dBm",
        "settings": {"rbw_hz": 10_000},
    }


class DetectionRegionTests(unittest.TestCase):
    def test_contiguous_regions_preserve_dbm_frequency_bandwidth_and_score(self) -> None:
        payload = spectral_sweep("session", "spectrum", "regions")
        sweep = Sweep.from_dict(payload)
        regions = detect_regions(sweep, threshold_dbm=-60.0, maximum=10)
        self.assertEqual(len(regions), 2)
        self.assertEqual(regions[0]["center_frequency_hz"], 100_200_000)
        self.assertEqual(regions[0]["bandwidth_hz"], 100_000)
        self.assertEqual(regions[0]["peak_level"], -45.0)
        self.assertEqual(regions[0]["score"], 15.0)
        self.assertEqual(regions[1]["center_frequency_hz"], 100_500_000)
        self.assertEqual(regions[1]["bandwidth_hz"], 100_000)
        self.assertEqual(detect_regions(sweep, threshold_dbm=-60.0, maximum=1), regions[:1])

    def test_non_dbm_and_single_point_bandwidth_are_explicit(self) -> None:
        payload = spectral_sweep("session", "spectrum", "single")
        payload["levels"] = [-90.0] * 7
        payload["levels"][3] = -40.0
        region = detect_regions(
            Sweep.from_dict(payload), threshold_dbm=-60.0, maximum=1
        )[0]
        self.assertEqual(region["bandwidth_hz"], 100_000)
        payload["level_unit"] = "s_meter_count"
        with self.assertRaisesRegex(ValueError, "только исходные dBm"):
            detect_regions(Sweep.from_dict(payload), threshold_dbm=-60.0, maximum=1)


class AutomaticCandidateDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService(history_limit=5000)
        self.session = self.service.create_session(simulated_session_payload())
        self.service.start_session(self.session.id)
        self.spectrum_id, self.receiver_id = [item.id for item in self.session.streams]
        self.detector = AutomaticCandidateDetector(
            self.service, start_background=False
        )

    def tearDown(self) -> None:
        self.detector.close()
        self.service.close()

    def enable(self, **policy):
        return self.detector.configure(
            self.session.id, {"enabled": True, "threshold_dbm": -60.0, **policy}
        )

    def ingest(self, sweep_id: str, number: int = 1):
        return self.service.ingest_sweep(
            spectral_sweep(self.session.id, self.spectrum_id, sweep_id, number)
        )

    def test_default_off_and_enable_do_not_reprocess_archive(self) -> None:
        self.ingest("before-opt-in")
        self.assertEqual(self.detector.run_once(self.session.id), [])
        self.assertEqual(self.service.list_candidates(self.session.id), [])

        status = self.enable()
        self.assertFalse(status["policy_persistent"])
        self.assertTrue(status["restart_requires_opt_in"])
        self.assertFalse(status["processes_existing_archive"])
        self.assertFalse(status["automatically_enqueues_investigations"])
        self.assertEqual(self.detector.run_once(self.session.id), [])
        self.assertEqual(self.service.list_candidates(self.session.id), [])

        replacement = AutomaticCandidateDetector(self.service, start_background=False)
        self.assertFalse(replacement.status(self.session.id)["policy"]["enabled"])
        replacement.close()

    def test_new_sweep_creates_candidate_once_with_provenance_and_no_handoff(self) -> None:
        self.enable(dedup_bandwidth_hz=150_000)
        created = self.detector.run_once(self.session.id)
        self.assertEqual(created, [])
        candidates = self.detector.run_once(self.session.id)
        self.assertEqual(candidates, [])
        self.ingest("detect-me")
        created = self.detector.run_once(self.session.id)
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].center_frequency_hz, 100_200_000)
        self.assertEqual(created[0].level_unit, "dBm")
        self.assertEqual(created[0].state, CandidateState.NEW)
        self.assertEqual(self.service.list_investigations(self.session.id), [])

        self.assertEqual(self.detector.run_once(self.session.id), [])
        self.ingest("same-signal-again", 2)
        self.assertEqual(self.detector.run_once(self.session.id), [])
        self.assertEqual(len(self.service.list_candidates(self.session.id)), 2)
        detection_events = [
            item for item in self.service.events(limit=5000)
            if item.event_type == "automatic_detection.candidate_created"
        ]
        self.assertEqual(len(detection_events), 2)
        self.assertEqual(detection_events[0].payload["sweep_id"], "detect-me")
        status = self.detector.status(self.session.id)
        self.assertEqual(status["runtime"]["candidates_created"], 2)
        self.assertEqual(status["runtime"]["duplicates_suppressed"], 2)
        self.assertEqual(status["runtime"]["sweeps_processed"], 2)

    def test_dismissed_candidate_is_not_recreated(self) -> None:
        self.enable()
        self.ingest("dismiss-source")
        candidate = self.detector.run_once(self.session.id)[0]
        self.service.dismiss_candidate(self.session.id, candidate.id)
        self.ingest("after-dismiss", 2)
        created = self.detector.run_once(self.session.id)
        remaining = [
            item for item in created
            if item.center_frequency_hz == candidate.center_frequency_hz
        ]
        self.assertEqual(remaining, [])
        self.assertEqual(
            self.service.get_candidate(self.session.id, candidate.id).state,
            CandidateState.DISMISSED,
        )

    def test_stream_filter_policy_validation_and_stopped_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "только spectrum_analyzer"):
            self.enable(stream_id=self.receiver_id)
        with self.assertRaisesRegex(ValueError, "JSON boolean"):
            self.detector.configure(self.session.id, {"enabled": 1})
        with self.assertRaisesRegex(ValueError, "Неизвестные поля"):
            self.detector.configure(self.session.id, {"enable": True})
        with self.assertRaisesRegex(ValueError, "threshold_dbm"):
            self.enable(threshold_dbm=float("nan"))

        payload = simulated_session_payload()
        payload["streams"][0]["level_unit"] = "s_meter_count"
        raw_session = self.service.create_session(payload)
        with self.assertRaisesRegex(ValueError, "только поток с исходными dBm"):
            self.detector.configure(
                raw_session.id,
                {"enabled": True, "stream_id": raw_session.streams[0].id},
            )

        self.enable(stream_id=self.spectrum_id, max_candidates_per_sweep=1)
        self.ingest("limited")
        created = self.detector.run_once(self.session.id)
        self.assertEqual(len(created), 1)
        self.service.stop_session(self.session.id)
        self.ingest("stopped-session", 2)
        self.assertEqual(self.detector.run_once(self.session.id), [])
        self.assertEqual(
            self.detector.status(self.session.id)["runtime"]["last_result"],
            "session_not_running",
        )

    def test_sqlite_archive_uses_same_detection_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "sessions.sqlite")
            store = SQLiteSessionStore(path)
            service = SessionService(store=store)
            detector = AutomaticCandidateDetector(service, start_background=False)
            try:
                session = service.create_session(simulated_session_payload())
                service.start_session(session.id)
                detector.configure(
                    session.id, {"enabled": True, "threshold_dbm": -60.0}
                )
                service.ingest_sweep(
                    spectral_sweep(session.id, session.streams[0].id, "persistent")
                )
                created = detector.run_once(session.id)
                self.assertEqual(len(created), 2)
                self.assertEqual(len(service.list_candidates(session.id)), 2)
                self.assertTrue(
                    all(item.level_unit == "dBm" for item in created)
                )
            finally:
                detector.close()
                service.close()


class AutomaticDetectionHttpTests(unittest.TestCase):
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
        status = response.status
        raw = response.read()
        connection.close()
        return status, json.loads(raw.decode("utf-8"))

    def test_http_policy_round_trip_and_new_sweep_detection(self) -> None:
        status, created = self.request(
            "POST", "/api/v1/sessions", simulated_session_payload()
        )
        self.assertEqual(status, 201)
        session = created["data"]
        session_id = session["id"]
        stream_id = session["streams"][0]["id"]
        self.request("POST", f"/api/v1/sessions/{session_id}/start", {})
        path = f"/api/v1/sessions/{session_id}/automatic-detection"

        status, initial = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertFalse(initial["data"]["policy"]["enabled"])
        self.assertFalse(initial["data"]["policy_persistent"])
        status, rejected = self.request("POST", path, {"enabled": 1})
        self.assertEqual(status, 400)
        status, enabled = self.request(
            "POST",
            path,
            {
                "enabled": True,
                "interval_seconds": 0.1,
                "stream_id": stream_id,
                "threshold_dbm": -60.0,
                "max_candidates_per_sweep": 2,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(enabled["data"]["policy"]["enabled"])
        status, _ = self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/sweeps",
            spectral_sweep(session_id, stream_id, "http-detection"),
        )
        self.assertEqual(status, 201)
        wait_until(
            lambda: len(self.server.session_service.list_candidates(session_id)) == 2,
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
        self.assertFalse(features["automatic_investigation_completion"])


class AutomaticDetectionBackgroundTests(unittest.TestCase):
    def test_background_waits_for_opt_in_and_stops_after_disable(self) -> None:
        service = SessionService(history_limit=5000)
        detector = AutomaticCandidateDetector(service)
        try:
            session = service.create_session(simulated_session_payload())
            service.start_session(session.id)
            stream_id = session.streams[0].id
            service.ingest_sweep(spectral_sweep(session.id, stream_id, "background-off"))
            detector.configure(
                session.id,
                {"enabled": True, "interval_seconds": 0.1, "threshold_dbm": -60.0},
            )
            service.ingest_sweep(spectral_sweep(session.id, stream_id, "background-on", 2))
            wait_until(
                lambda: len(service.list_candidates(session.id)) == 2,
                "После opt-in detector должен создать два кандидата",
            )
            detector.configure(session.id, {"enabled": False})
            service.ingest_sweep(spectral_sweep(session.id, stream_id, "background-disabled", 3))
            self.assertEqual(detector.run_once(session.id), [])
            self.assertEqual(len(service.list_candidates(session.id)), 2)
        finally:
            detector.close()
            service.close()

    def test_blocked_archive_read_does_not_serialize_another_session(self) -> None:
        service = SessionService(history_limit=5000)
        original = service.list_sweeps
        entered = threading.Event()
        release = threading.Event()
        first = second = None
        blocking = False

        def list_sweeps(session_id, **kwargs):
            if blocking and session_id == first.id:
                entered.set()
                if not release.wait(timeout=4):
                    raise RuntimeError("Стенд: не дождался разблокировки архива")
            return original(session_id, **kwargs)

        service.list_sweeps = list_sweeps
        detector = AutomaticCandidateDetector(service)
        try:
            first = service.create_session(simulated_session_payload())
            second = service.create_session(simulated_session_payload())
            for session, label in ((first, "first"), (second, "second")):
                service.start_session(session.id)
                detector.configure(
                    session.id,
                    {"enabled": True, "interval_seconds": 0.1, "threshold_dbm": -60.0},
                )
                service.ingest_sweep(
                    spectral_sweep(session.id, session.streams[0].id, label)
                )
            blocking = True
            self.assertTrue(entered.wait(timeout=2), "Первый сеанс не заблокировал архив")
            try:
                wait_until(
                    lambda: len(service.list_candidates(second.id)) == 2,
                    "Медленное чтение первого сеанса не должно задерживать второй",
                    timeout=1.5,
                )
            finally:
                release.set()
            wait_until(
                lambda: len(service.list_candidates(first.id)) == 2,
                "Первый сеанс должен обработаться после разблокировки",
            )
        finally:
            release.set()
            detector.close()
            service.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
