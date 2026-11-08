"""Проверка SQLite-архива, восстановления и HTTP-приёма измерений."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from radioinspector.contracts import TimeInterval, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_platform import BASE, session_payload


def measurement_interval(seconds: int = 0) -> dict[str, str]:
    interval = TimeInterval(BASE + timedelta(seconds=seconds), BASE + timedelta(seconds=seconds + 1))
    return to_primitive(interval)


def sweep_payload(session_id: str, stream_id: str, number: int = 0) -> dict:
    return {
        "id": f"sweep-{number}",
        "session_id": session_id,
        "stream_id": stream_id,
        "interval": measurement_interval(number),
        "frequencies_hz": [100_000_000 + number, 100_100_000 + number],
        "levels": [-80.5, -42.0],
        "level_unit": "dBm",
        "settings": {"rbw_hz": 10_000},
    }


def observation_payload(session_id: str, stream_id: str, number: int = 0) -> dict:
    return {
        "id": f"observation-{number}",
        "session_id": session_id,
        "stream_id": stream_id,
        "interval": measurement_interval(number),
        "frequency_hz": 145_500_000 + number,
        "mode": "FM",
        "level": 37,
        "level_unit": "s_meter_count",
        "squelch_open": True,
        "decoded_mode": "DMR",
        "metadata": {"source": "test"},
    }


class SQLiteSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temporary.name) / "nested" / "sessions.sqlite")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self) -> SessionService:
        return SessionService(store=SQLiteSessionStore(self.path))

    def create_two_device_session(self, service: SessionService):
        session = service.create_session(session_payload())
        return session, session.streams[0].id, session.streams[1].id

    def test_capabilities_and_session_lifecycle_survive_restart(self) -> None:
        service = self.service()
        session, spectrum_id, receiver_id = self.create_two_device_session(service)
        service.start_session(session.id, at=BASE)
        self.assertEqual(service.capabilities()["session_storage"], "sqlite")
        self.assertTrue(service.capabilities()["persistent_archive"])
        self.assertTrue(service.capabilities()["features"]["session_recovery"])
        service.close()

        restored = self.service()
        try:
            session = restored.get_session(session.id)
            self.assertEqual(session.state.value, "running")
            self.assertEqual(session.started_at, BASE)
            self.assertEqual([item.id for item in session.streams], [spectrum_id, receiver_id])
            self.assertEqual(session.streams[0].level_unit, "dBm")
            self.assertEqual(session.streams[1].level_unit, "s_meter_count")
            self.assertEqual(
                [item.event_type for item in restored.events()],
                ["session.created", "session.started"],
            )
            stopped = restored.stop_session(session.id, at=BASE + timedelta(seconds=5))
            self.assertEqual(stopped.state.value, "stopped")
            self.assertEqual(restored.health()["last_event_id"], 3)
        finally:
            restored.close()

    def test_measurements_and_raw_units_survive_restart(self) -> None:
        service = self.service()
        session, spectrum_id, receiver_id = self.create_two_device_session(service)
        service.ingest_sweep(sweep_payload(session.id, spectrum_id))
        service.ingest_observation(observation_payload(session.id, receiver_id))
        service.close()

        restored = self.service()
        try:
            sweeps = restored.list_sweeps(session.id)
            observations = restored.list_observations(session.id)
            self.assertEqual(len(sweeps), 1)
            self.assertEqual(sweeps[0].frequencies_hz, (100_000_000, 100_100_000))
            self.assertEqual(sweeps[0].levels, (-80.5, -42.0))
            self.assertEqual(sweeps[0].level_unit, "dBm")
            self.assertEqual(sweeps[0].settings["rbw_hz"], 10_000)
            self.assertEqual(observations[0].level, 37.0)
            self.assertEqual(observations[0].level_unit, "s_meter_count")
            self.assertIs(observations[0].squelch_open, True)
            self.assertEqual(observations[0].decoded_mode, "DMR")
            self.assertFalse(hasattr(observations[0], "level_dbm"))
            self.assertEqual(
                [item.event_type for item in restored.events(after=1)],
                ["sweep.received", "observation.received"],
            )
        finally:
            restored.close()

    def test_two_device_writers_are_serialized_without_crossing_streams(self) -> None:
        service = self.service()
        session, spectrum_id, receiver_id = self.create_two_device_session(service)

        def write_sweep(number: int) -> None:
            service.ingest_sweep(sweep_payload(session.id, spectrum_id, number))

        def write_observation(number: int) -> None:
            service.ingest_observation(observation_payload(session.id, receiver_id, number))

        with ThreadPoolExecutor(max_workers=8) as executor:
            jobs = [executor.submit(write_sweep, number) for number in range(12)]
            jobs += [executor.submit(write_observation, number) for number in range(12)]
            for job in jobs:
                job.result(timeout=5)

        try:
            sweeps = service.list_sweeps(session.id, stream_id=spectrum_id, limit=100)
            observations = service.list_observations(
                session.id, stream_id=receiver_id, limit=100
            )
            self.assertEqual(len(sweeps), 12)
            self.assertEqual(len(observations), 12)
            self.assertTrue(all(item.stream_id == spectrum_id for item in sweeps))
            self.assertTrue(all(item.stream_id == receiver_id for item in observations))
            self.assertEqual(service.health()["last_event_id"], 25)
        finally:
            service.close()

    def test_rejects_unit_conversion_foreign_stream_and_duplicate_id(self) -> None:
        service = self.service()
        session, spectrum_id, receiver_id = self.create_two_device_session(service)
        try:
            wrong_unit = sweep_payload(session.id, spectrum_id)
            wrong_unit["level_unit"] = "s_meter_count"
            with self.assertRaisesRegex(ValueError, "преобразование"):
                service.ingest_sweep(wrong_unit)

            foreign_stream = sweep_payload(session.id, "foreign")
            with self.assertRaisesRegex(ValueError, "не принадлежит"):
                service.ingest_sweep(foreign_stream)

            service.ingest_sweep(sweep_payload(session.id, spectrum_id))
            with self.assertRaisesRegex(ValueError, "уже существует"):
                service.ingest_sweep(sweep_payload(session.id, spectrum_id))
            self.assertEqual(len(service.list_sweeps(session.id)), 1)
            self.assertEqual(service.health()["last_event_id"], 2)
        finally:
            service.close()

    def test_memory_mode_also_ingests_without_claiming_persistence(self) -> None:
        service = SessionService()
        session, spectrum_id, receiver_id = self.create_two_device_session(service)
        service.ingest_sweep(sweep_payload(session.id, spectrum_id))
        service.ingest_observation(observation_payload(session.id, receiver_id))
        self.assertEqual(len(service.list_sweeps(session.id)), 1)
        self.assertEqual(len(service.list_observations(session.id)), 1)
        self.assertEqual(service.capabilities()["session_storage"], "memory")
        self.assertFalse(service.capabilities()["persistent_archive"])
        self.assertTrue(service.capabilities()["features"]["measurement_ingest"])
        with self.assertRaises(ValueError):
            service.list_sweeps(session.id, limit=0)


class BatchIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temporary.name) / "batch-sessions.sqlite")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self) -> SessionService:
        return SessionService(store=SQLiteSessionStore(self.path))

    def create_two_device_session(self, service: SessionService):
        session = service.create_session(session_payload())
        return session, session.streams[0].id, session.streams[1].id

    def test_memory_batches_preserve_order_units_and_event_sequences(self) -> None:
        service = SessionService()
        session, spectrum_id, receiver_id = self.create_two_device_session(service)
        sweeps = service.ingest_sweeps([
            sweep_payload(session.id, spectrum_id, number) for number in range(3)
        ])
        observations = service.ingest_observations([
            observation_payload(session.id, receiver_id, number) for number in range(2)
        ])
        self.assertEqual([item.id for item in sweeps], ["sweep-0", "sweep-1", "sweep-2"])
        self.assertTrue(all(item.level_unit == "dBm" for item in sweeps))
        self.assertTrue(all(item.level_unit == "s_meter_count" for item in observations))
        events = service.events(after=1)
        self.assertEqual([item.sequence for item in events], [2, 3, 4, 5, 6])
        self.assertEqual(
            [item.event_type for item in events],
            ["sweep.received"] * 3 + ["observation.received"] * 2,
        )
        self.assertEqual(service.health()["last_event_id"], 6)
        features = service.capabilities()["features"]
        self.assertTrue(features["measurement_batch_ingest"])
        self.assertTrue(features["measurement_batch_atomic"])
        self.assertEqual(features["measurement_batch_limit"], 100)

    def test_invalid_memory_batch_has_no_partial_measurements_or_events(self) -> None:
        service = SessionService()
        session, spectrum_id, _receiver_id = self.create_two_device_session(service)
        valid = sweep_payload(session.id, spectrum_id, 1)
        duplicate = sweep_payload(session.id, spectrum_id, 1)
        with self.assertRaisesRegex(ValueError, "внутри пакета"):
            service.ingest_sweeps([valid, duplicate])
        self.assertEqual(service.list_sweeps(session.id), [])
        self.assertEqual(service.events(after=1), [])
        for payloads in ([], [sweep_payload(session.id, spectrum_id)] * 101):
            with self.subTest(size=len(payloads)):
                with self.assertRaisesRegex(ValueError, "от 1 до 100"):
                    service.ingest_sweeps(payloads)
        with self.assertRaises(TypeError):
            service.ingest_observations({"items": []})

    def test_sqlite_batch_rolls_back_measurements_and_events_on_duplicate(self) -> None:
        for kind in ("sweeps", "observations"):
            with self.subTest(kind=kind):
                self.path = str(Path(self.temporary.name) / f"batch-{kind}.sqlite")
                service = self.service()
                session, spectrum_id, receiver_id = self.create_two_device_session(service)
                if kind == "sweeps":
                    service.ingest_sweep(sweep_payload(session.id, spectrum_id, 0))
                    batch = [
                        sweep_payload(session.id, spectrum_id, 1),
                        sweep_payload(session.id, spectrum_id, 0),
                    ]
                    with self.assertRaisesRegex(ValueError, "Пакет свипов"):
                        service.ingest_sweeps(batch)
                else:
                    service.ingest_observation(observation_payload(session.id, receiver_id, 0))
                    batch = [
                        observation_payload(session.id, receiver_id, 1),
                        observation_payload(session.id, receiver_id, 0),
                    ]
                    with self.assertRaisesRegex(ValueError, "Пакет наблюдений"):
                        service.ingest_observations(batch)
                service.close()

                restored = self.service()
                try:
                    if kind == "sweeps":
                        self.assertEqual(
                            [item.id for item in restored.list_sweeps(session.id)],
                            ["sweep-0"],
                        )
                    else:
                        self.assertEqual(
                            [item.id for item in restored.list_observations(session.id)],
                            ["observation-0"],
                        )
                    self.assertEqual(restored.health()["last_event_id"], 2)
                    self.assertEqual(len(restored.events(after=1)), 1)
                finally:
                    restored.close()


class PersistentHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temporary.name) / "http-sessions.sqlite")
        self.service: SessionService | None = None
        self.server = None
        self.thread = None
        self.start_server()

    def start_server(self) -> None:
        self.service = SessionService(store=SQLiteSessionStore(self.path))
        self.server = create_server(host="127.0.0.1", port=0, sessions=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def stop_server(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.service is not None:
            self.service.close()
        self.server = None
        self.thread = None
        self.service = None

    def tearDown(self) -> None:
        self.stop_server()
        self.temporary.cleanup()

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
        status, response = self.request("POST", "/api/v1/sessions", session_payload())
        self.assertEqual(status, 201)
        data = response["data"]
        return data["id"], data["streams"][0]["id"], data["streams"][1]["id"]

    def test_http_ingest_query_and_restart_recovery(self) -> None:
        session_id, spectrum_id, receiver_id = self.create_session()
        sweep = sweep_payload(session_id, spectrum_id)
        del sweep["session_id"]
        status, created_sweep = self.request(
            "POST", f"/api/v1/sessions/{session_id}/sweeps", sweep
        )
        self.assertEqual(status, 201)
        self.assertEqual(created_sweep["data"]["session_id"], session_id)

        status, created_observation = self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/observations",
            observation_payload(session_id, receiver_id),
        )
        self.assertEqual(status, 201)
        self.assertEqual(created_observation["data"]["level_unit"], "s_meter_count")

        status, listing = self.request(
            "GET", f"/api/v1/sessions/{session_id}/sweeps?stream_id={spectrum_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["data"]), 1)
        self.assertEqual(listing["data"][0]["level_unit"], "dBm")

        self.stop_server()
        self.start_server()

        status, restored_session = self.request("GET", f"/api/v1/sessions/{session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(restored_session["data"]["id"], session_id)
        status, observations = self.request(
            "GET", f"/api/v1/sessions/{session_id}/observations"
        )
        self.assertEqual(status, 200)
        self.assertEqual(observations["data"][0]["decoded_mode"], "DMR")
        self.assertIs(observations["data"][0]["squelch_open"], True)
        status, history = self.request("GET", "/api/v1/events/history?after=1")
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["event_type"] for item in history["data"]],
            ["sweep.received", "observation.received"],
        )

    def test_http_rejects_mismatched_route_unit_and_foreign_stream(self) -> None:
        session_id, spectrum_id, _receiver_id = self.create_session()
        mismatched = sweep_payload("another-session", spectrum_id)
        status, error = self.request(
            "POST", f"/api/v1/sessions/{session_id}/sweeps", mismatched
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "invalid_request")

        wrong_unit = sweep_payload(session_id, spectrum_id)
        wrong_unit["level_unit"] = "s_meter_count"
        status, error = self.request(
            "POST", f"/api/v1/sessions/{session_id}/sweeps", wrong_unit
        )
        self.assertEqual(status, 400)
        self.assertIn("преобразование", error["error"]["message"])

        foreign = sweep_payload(session_id, "foreign")
        status, error = self.request(
            "POST", f"/api/v1/sessions/{session_id}/sweeps", foreign
        )
        self.assertEqual(status, 400)
        self.assertIn("не принадлежит", error["error"]["message"])

        status, error = self.request("GET", "/api/v1/sessions/missing/sweeps")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"]["code"], "session_not_found")

    def test_http_batch_ingest_and_restart(self) -> None:
        session_id, spectrum_id, receiver_id = self.create_session()
        sweeps = [sweep_payload(session_id, spectrum_id, number) for number in range(2)]
        del sweeps[0]["session_id"]
        status, response = self.request(
            "POST", f"/api/v1/sessions/{session_id}/sweeps/batch", {"items": sweeps}
        )
        self.assertEqual(status, 201)
        self.assertEqual(response["data"]["count"], 2)
        self.assertEqual(
            [item["session_id"] for item in response["data"]["items"]],
            [session_id, session_id],
        )
        status, response = self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/observations/batch",
            {"items": [observation_payload(session_id, receiver_id, number) for number in range(2)]},
        )
        self.assertEqual(status, 201)
        self.assertEqual(response["data"]["count"], 2)
        self.assertTrue(
            all(item["level_unit"] == "s_meter_count" for item in response["data"]["items"])
        )

        self.stop_server()
        self.start_server()
        status, restored = self.request(
            "GET", f"/api/v1/sessions/{session_id}/sweeps?stream_id={spectrum_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(restored["data"]), 2)
        status, history = self.request("GET", "/api/v1/events/history?after=1")
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["event_type"] for item in history["data"]],
            ["sweep.received"] * 2 + ["observation.received"] * 2,
        )

    def test_http_invalid_batch_is_atomic(self) -> None:
        session_id, spectrum_id, _receiver_id = self.create_session()
        valid = sweep_payload(session_id, spectrum_id, 1)
        invalid = sweep_payload(session_id, spectrum_id, 2)
        invalid["level_unit"] = "s_meter_count"
        status, error = self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/sweeps/batch",
            {"items": [valid, invalid]},
        )
        self.assertEqual(status, 400)
        self.assertIn("преобразование", error["error"]["message"])
        status, listing = self.request("GET", f"/api/v1/sessions/{session_id}/sweeps")
        self.assertEqual(status, 200)
        self.assertEqual(listing["data"], [])
        status, health = self.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["data"]["last_event_id"], 1)
        status, error = self.request(
            "POST", f"/api/v1/sessions/{session_id}/sweeps/batch", {"items": []}
        )
        self.assertEqual(status, 400)
        self.assertIn("от 1 до 100", error["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
