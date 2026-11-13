"""Тесты общего контракта, службы сеансов и HTTP/SSE API v1."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radioinspector.contracts import (  # noqa: E402
    Candidate,
    CandidateState,
    DeviceRole,
    Investigation,
    MonitoringSession,
    ReceiverObservation,
    SessionState,
    Sweep,
    TimeInterval,
    parse_time,
    to_primitive,
)
from radioinspector.session_service import (  # noqa: E402
    SessionNotFoundError,
    SessionService,
)
from radioinspector.web.server import create_server  # noqa: E402


BASE = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)


def interval(seconds: int = 1) -> TimeInterval:
    return TimeInterval(BASE, BASE + timedelta(seconds=seconds))


def session_payload() -> dict:
    return {
        "name": "Двойной обзор",
        "streams": [
            {
                "device": "tinySA Ultra",
                "role": "spectrum_analyzer",
                "level_unit": "dBm",
                "port": "/dev/ttyACM0",
                "capabilities": ["sweep", "waterfall"],
            },
            {
                "device": "AR-DV1",
                "role": "receiver",
                "level_unit": "s_meter_count",
                "port": "/dev/ttyUSB0",
                "capabilities": ["tune", "audio", "hardware_decode"],
            },
        ],
        "metadata": {"antennas": "independent"},
    }


class ContractTests(unittest.TestCase):
    def test_two_device_session_round_trip(self) -> None:
        session = MonitoringSession.from_dict(session_payload())
        payload = to_primitive(session)
        self.assertEqual(payload["contract_version"], 1)
        self.assertTrue(payload["created_at"].endswith("Z"))
        self.assertEqual(len(payload["streams"]), 2)
        self.assertEqual(payload["streams"][0]["level_unit"], "dBm")
        self.assertEqual(payload["streams"][1]["level_unit"], "s_meter_count")

        restored = MonitoringSession.from_dict(payload)
        self.assertEqual(restored.id, session.id)
        self.assertEqual(restored.streams[1].role, DeviceRole.RECEIVER)
        self.assertEqual(restored.metadata["antennas"], "independent")

    def test_session_rejects_duplicate_port(self) -> None:
        payload = session_payload()
        payload["streams"][1]["port"] = payload["streams"][0]["port"]
        with self.assertRaisesRegex(ValueError, "порт"):
            MonitoringSession.from_dict(payload)

    def test_session_lifecycle(self) -> None:
        session = MonitoringSession.from_dict(session_payload())
        session.start(BASE)
        session.stop(BASE + timedelta(seconds=10))
        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertEqual(session.started_at, BASE)
        self.assertEqual(session.ended_at, BASE + timedelta(seconds=10))
        with self.assertRaises(ValueError):
            session.stop(BASE + timedelta(seconds=11))

    def test_interval_requires_utc_and_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "часовой пояс"):
            TimeInterval(datetime(2026, 1, 1), datetime(2026, 1, 1, 0, 1))
        with self.assertRaisesRegex(ValueError, "предшествовать"):
            TimeInterval(BASE + timedelta(seconds=1), BASE)
        self.assertEqual(parse_time("2026-01-01T00:00:00Z").tzinfo, timezone.utc)

    def test_sweep_preserves_raw_unit_and_integer_hz(self) -> None:
        sweep = Sweep(
            session_id="session",
            stream_id="stream",
            interval=interval(),
            frequencies_hz=(100_000_000, 100_100_000),
            levels=(-80.5, -42.0),
            level_unit="dBm",
        )
        restored = Sweep.from_dict(to_primitive(sweep))
        self.assertEqual(restored.level_unit, "dBm")
        self.assertEqual(restored.frequencies_hz, (100_000_000, 100_100_000))
        self.assertEqual(restored.levels, (-80.5, -42.0))
        with self.assertRaises(TypeError):
            Sweep(
                session_id="session",
                stream_id="stream",
                interval=interval(),
                frequencies_hz=(100_000_000.5,),
                levels=(-40.0,),
                level_unit="dBm",
            )

    def test_sweep_rejects_mismatched_arrays_and_nonfinite_level(self) -> None:
        with self.assertRaisesRegex(ValueError, "одной длины"):
            Sweep(
                session_id="session",
                stream_id="stream",
                interval=interval(),
                frequencies_hz=(1, 2),
                levels=(-40.0,),
                level_unit="dBm",
            )
        with self.assertRaisesRegex(ValueError, "конечным"):
            Sweep(
                session_id="session",
                stream_id="stream",
                interval=interval(),
                frequencies_hz=(1,),
                levels=(float("nan"),),
                level_unit="dBm",
            )

    def test_receiver_observation_does_not_convert_s_meter(self) -> None:
        observation = ReceiverObservation(
            session_id="session",
            stream_id="stream",
            interval=interval(),
            frequency_hz=145_500_000,
            mode="FM",
            level=37,
            level_unit="s_meter_count",
            decoded_mode="DMR",
        )
        payload = to_primitive(observation)
        self.assertEqual(payload["level"], 37.0)
        self.assertEqual(payload["level_unit"], "s_meter_count")
        self.assertNotIn("level_dbm", payload)
        self.assertEqual(ReceiverObservation.from_dict(payload).decoded_mode, "DMR")

    def test_candidate_and_investigation_round_trip(self) -> None:
        candidate = Candidate(
            session_id="session",
            source_stream_id="tinysa",
            interval=interval(),
            center_frequency_hz=145_500_000,
            bandwidth_hz=25_000,
            peak_level=-45.0,
            level_unit="dBm",
        )
        investigation = Investigation(
            candidate_id=candidate.id,
            target_stream_id="receiver",
            target_frequency_hz=candidate.center_frequency_hz,
            priority=10,
            mode="FM",
        )
        restored_candidate = Candidate.from_dict(to_primitive(candidate))
        restored_investigation = Investigation.from_dict(to_primitive(investigation))
        self.assertEqual(restored_candidate.state, CandidateState.NEW)
        self.assertEqual(restored_investigation.candidate_id, candidate.id)
        self.assertEqual(restored_investigation.priority, 10)


class SessionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [BASE]
        self.service = SessionService(clock=lambda: self.now[0])

    def test_lifecycle_and_event_history(self) -> None:
        session = self.service.create_session(session_payload())
        self.assertEqual(session.state, SessionState.DRAFT)
        self.now[0] = BASE + timedelta(seconds=1)
        started = self.service.start_session(session.id)
        self.assertEqual(started.state, SessionState.RUNNING)
        self.now[0] = BASE + timedelta(seconds=2)
        stopped = self.service.stop_session(session.id)
        self.assertEqual(stopped.state, SessionState.STOPPED)

        events = self.service.events()
        self.assertEqual(
            [item.event_type for item in events],
            ["session.created", "session.started", "session.stopped"],
        )
        self.assertEqual([item.sequence for item in events], [1, 2, 3])
        self.assertEqual(self.service.health()["running_sessions"], 0)
        self.assertEqual(self.service.health()["sessions"], 1)

    def test_replay_subscription_has_no_history_live_gap(self) -> None:
        session = self.service.create_session(session_payload())
        self.service.start_session(session.id)
        subscriber_id, events = self.service.subscribe(after=1)
        try:
            replayed = events.get_nowait()
            self.assertEqual(replayed.sequence, 2)
            self.assertEqual(replayed.event_type, "session.started")
            self.service.stop_session(session.id)
            live = events.get_nowait()
            self.assertEqual(live.sequence, 3)
            self.assertEqual(live.event_type, "session.stopped")
        finally:
            self.service.unsubscribe(subscriber_id)

    def test_returned_session_is_isolated_copy(self) -> None:
        session = self.service.create_session(session_payload())
        session.name = "Изменено снаружи"
        self.assertEqual(self.service.get_session(session.id).name, "Двойной обзор")

    def test_unknown_session_has_specific_error(self) -> None:
        with self.assertRaises(SessionNotFoundError):
            self.service.get_session("missing")

    def test_capabilities_are_honest_about_memory_storage(self) -> None:
        capabilities = self.service.capabilities()
        self.assertEqual(capabilities["session_storage"], "memory")
        self.assertFalse(capabilities["persistent_archive"])
        self.assertFalse(capabilities["features"]["hardware_control"])
        self.assertTrue(capabilities["features"]["independent_device_streams"])


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService()
        self.server = create_server(
            host="127.0.0.1", port=0, sessions=self.service
        )
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw.decode("utf-8"))

    def create_session(self) -> str:
        status, response = self.request("POST", "/api/v1/sessions", session_payload())
        self.assertEqual(status, 201)
        return response["data"]["id"]

    def test_health_and_capabilities(self) -> None:
        status, health = self.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["data"]["status"], "ok")
        self.assertEqual(health["contract_version"], 1)

        status, capabilities = self.request("GET", "/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(capabilities["data"]["hardware_ownership"], "core")
        self.assertIn("raspberry-pi-webui", capabilities["data"]["clients"])

    def test_session_rest_lifecycle(self) -> None:
        session_id = self.create_session()
        status, listing = self.request("GET", "/api/v1/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["data"]), 1)
        self.assertEqual(len(listing["data"][0]["streams"]), 2)

        status, started = self.request(
            "POST", f"/api/v1/sessions/{session_id}/start", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(started["data"]["state"], "running")

        status, stopped = self.request(
            "POST", f"/api/v1/sessions/{session_id}/stop", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(stopped["data"]["state"], "stopped")

        status, history = self.request("GET", "/api/v1/events/history?after=1")
        self.assertEqual(status, 200)
        self.assertEqual([item["sequence"] for item in history["data"]], [2, 3])

    def test_invalid_session_and_transition_return_json_errors(self) -> None:
        status, missing = self.request("GET", "/api/v1/sessions/missing")
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"]["code"], "session_not_found")

        session_id = self.create_session()
        status, invalid = self.request(
            "POST", f"/api/v1/sessions/{session_id}/stop", {}
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_request")

        status, missing_name = self.request("POST", "/api/v1/sessions", {})
        self.assertEqual(status, 400)
        self.assertEqual(missing_name["error"]["code"], "missing_field")

    def read_sse_frame(
        self, path: str, headers: dict | None = None
    ) -> tuple[http.client.HTTPConnection, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        frame: dict[str, str] = {}
        while True:
            line = response.readline().decode("utf-8").strip()
            if not line:
                if frame:
                    break
                continue
            if line.startswith(":"):
                continue
            key, value = line.split(":", 1)
            frame[key] = value.strip()
        frame["json"] = json.loads(frame["data"])
        return connection, frame

    def test_sse_replays_created_session(self) -> None:
        session_id = self.create_session()
        connection, frame = self.read_sse_frame("/api/v1/events")
        try:
            self.assertEqual(frame["id"], "1")
            self.assertEqual(frame["event"], "session.created")
            self.assertEqual(frame["json"]["data"]["session_id"], session_id)
        finally:
            connection.close()

    def test_sse_resumes_after_last_event_id(self) -> None:
        session_id = self.create_session()
        self.request("POST", f"/api/v1/sessions/{session_id}/start", {})
        connection, frame = self.read_sse_frame(
            "/api/v1/events", {"Last-Event-ID": "1"}
        )
        try:
            self.assertEqual(frame["id"], "2")
            self.assertEqual(frame["event"], "session.started")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
