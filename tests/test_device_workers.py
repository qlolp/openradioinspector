"""Симуляторы и независимые исполнители двухприборного сеанса."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable

from radioinspector.contracts import DeviceState
from radioinspector.device_workers import (
    ARDV1Simulator,
    DeviceWorkerManager,
    TinySASimulator,
    UnsupportedAdapterError,
    WorkerError,
    create_simulated_adapter,
)
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server


def simulated_session_payload(
    *, tinysa_settings: dict[str, Any] | None = None,
    ardv1_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": "Симулированный двойной обзор",
        "streams": [
            {
                "device": "tinySA Ultra",
                "role": "spectrum_analyzer",
                "level_unit": "dBm",
                "port": "sim://tinysa",
                "capabilities": ["sweep", "waterfall"],
                "settings": {
                    "adapter": "tinysa-sim",
                    "start_hz": 100_000_000,
                    "stop_hz": 106_000_000,
                    "points": 61,
                    "period_s": 0.01,
                    **(tinysa_settings or {}),
                },
            },
            {
                "device": "AR-DV1",
                "role": "receiver",
                "level_unit": "s_meter_count",
                "port": "sim://ardv1",
                "capabilities": ["tune", "audio", "hardware_decode"],
                "settings": {
                    "adapter": "ardv1-sim",
                    "frequency_hz": 145_500_000,
                    "mode": "FM",
                    "period_s": 0.01,
                    **(ardv1_settings or {}),
                },
            },
        ],
        "metadata": {"test_fixture": True},
    }


def wait_until(predicate: Callable[[], bool], message: str, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


class SimulatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService()
        self.session = self.service.create_session(simulated_session_payload())

    def test_tinysa_emits_integer_hz_dbm_and_output_off(self) -> None:
        adapter = create_simulated_adapter(self.session.streams[0])
        self.assertIsInstance(adapter, TinySASimulator)
        sweep = adapter.poll()
        self.assertEqual(sweep.level_unit, "dBm")
        self.assertEqual(len(sweep.frequencies_hz), 61)
        self.assertTrue(all(isinstance(item, int) for item in sweep.frequencies_hz))
        self.assertEqual(sweep.settings["generator_output"], "off")
        self.assertTrue(sweep.settings["simulated"])
        self.assertTrue(sweep.interval.ended_at.tzinfo is not None)

    def test_ardv1_tune_preserves_raw_s_meter_and_digital_mode(self) -> None:
        adapter = create_simulated_adapter(self.session.streams[1])
        self.assertIsInstance(adapter, ARDV1Simulator)
        tuned = adapter.execute("tune", {"frequency_hz": 433_075_000, "mode": "DMR"})
        self.assertEqual(tuned["frequency_hz"], 433_075_000)
        observation = adapter.poll()
        self.assertEqual(observation.frequency_hz, 433_075_000)
        self.assertEqual(observation.level_unit, "s_meter_count")
        self.assertEqual(observation.decoded_mode, "DMR")
        self.assertFalse(hasattr(observation, "level_dbm"))
        self.assertEqual(observation.metadata["tune_sequence"], 1)

    def test_adapter_is_explicit_and_validates_units(self) -> None:
        payload = simulated_session_payload()
        del payload["streams"][0]["settings"]["adapter"]
        session = self.service.create_session(payload)
        with self.assertRaises(UnsupportedAdapterError):
            create_simulated_adapter(session.streams[0])

        payload = simulated_session_payload()
        payload["streams"][0]["level_unit"] = "s_meter_count"
        session = self.service.create_session(payload)
        with self.assertRaisesRegex(ValueError, "dBm"):
            create_simulated_adapter(session.streams[0])


class WorkerManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService(history_limit=5000)
        self.manager = DeviceWorkerManager(self.service)

    def tearDown(self) -> None:
        self.manager.close()
        self.service.close()

    def start_session(self, payload: dict[str, Any] | None = None):
        session = self.service.create_session(payload or simulated_session_payload())
        self.service.start_session(session.id)
        return session, session.streams[0].id, session.streams[1].id

    def test_continuous_archive_and_stopping_one_device_does_not_stop_other(self) -> None:
        session, tinysa_id, ardv1_id = self.start_session(
            simulated_session_payload(ardv1_settings={"period_s": 0.2})
        )
        statuses = self.manager.start_session(session.id)
        self.assertEqual(len(statuses), 2)
        wait_until(
            lambda: len(self.service.list_sweeps(session.id)) >= 4
            and len(self.service.list_observations(session.id)) >= 4,
            "Оба прибора должны непрерывно писать в архив",
        )

        stopped = self.manager.stop_stream(session.id, tinysa_id)
        self.assertEqual(stopped["state"], "disconnected")
        sweep_count = len(self.service.list_sweeps(session.id))
        observation_count = len(self.service.list_observations(session.id))
        # Independent devices have different cadences. Prove an actual new
        # receiver commit instead of assuming it is scheduled within 80 ms.
        wait_until(
            lambda: len(self.service.list_observations(session.id)) > observation_count,
            "AR-DV1 должен продолжать запись после остановки tinySA",
        )
        self.assertEqual(len(self.service.list_sweeps(session.id)), sweep_count)
        self.assertGreater(len(self.service.list_observations(session.id)), observation_count)

        statuses = {item["stream_id"]: item for item in self.manager.list_workers(session.id)}
        self.assertFalse(statuses[tinysa_id]["alive"])
        self.assertTrue(statuses[ardv1_id]["alive"])
        self.manager.stop_session(session.id)
        self.assertEqual(self.manager.active_count, 0)

    def test_transient_failure_recovers_without_cross_device_effect(self) -> None:
        payload = simulated_session_payload(tinysa_settings={"failures_remaining": 1})
        session, tinysa_id, ardv1_id = self.start_session(payload)
        self.manager.start_session(session.id)
        wait_until(
            lambda: any(item.event_type == "device.error" for item in self.service.events(limit=5000))
            and len(self.service.list_sweeps(session.id)) >= 3
            and len(self.service.list_observations(session.id)) >= 3,
            "tinySA должен восстановиться, AR-DV1 — продолжить измерения",
        )
        statuses = {item["stream_id"]: item for item in self.manager.list_workers(session.id)}
        self.assertEqual(statuses[tinysa_id]["state"], "running")
        self.assertIn("Смоделированный сбой", statuses[tinysa_id]["last_error"])
        self.assertIsNone(statuses[ardv1_id]["last_error"])
        self.assertTrue(statuses[ardv1_id]["alive"])

    def test_fatal_failure_stops_only_failed_worker(self) -> None:
        payload = simulated_session_payload(
            tinysa_settings={"failures_remaining": 2, "max_consecutive_errors": 2}
        )
        session, tinysa_id, ardv1_id = self.start_session(payload)
        self.manager.start_session(session.id)

        def failed_and_receiver_active() -> bool:
            statuses = {item["stream_id"]: item for item in self.manager.list_workers(session.id)}
            return (
                statuses[tinysa_id]["state"] == "error"
                and not statuses[tinysa_id]["alive"]
                and statuses[ardv1_id]["alive"]
                and len(self.service.list_observations(session.id)) >= 3
            )

        wait_until(failed_and_receiver_active, "Фатальный сбой должен быть изолирован")
        statuses = {item["stream_id"]: item for item in self.manager.list_workers(session.id)}
        self.assertEqual(statuses[tinysa_id]["consecutive_errors"], 2)
        self.assertTrue(statuses[ardv1_id]["alive"])
        self.manager.stop_session(session.id)

    def test_tune_is_processed_by_receiver_queue(self) -> None:
        session, _tinysa_id, ardv1_id = self.start_session()
        self.manager.start_stream(session.id, ardv1_id)
        wait_until(
            lambda: len(self.service.list_observations(session.id)) >= 1,
            "Приёмник должен начать наблюдения",
        )
        result = self.manager.tune(
            session.id,
            ardv1_id,
            {"frequency_hz": 433_075_000, "mode": "DMR"},
        )
        self.assertEqual(result["tuned"]["frequency_hz"], 433_075_000)
        self.assertEqual(result["tuned"]["mode"], "DMR")
        wait_until(
            lambda: any(
                item.frequency_hz == 433_075_000 and item.decoded_mode == "DMR"
                for item in self.service.list_observations(session.id, limit=1000)
            ),
            "После команды должны появиться наблюдения на новой частоте",
        )
        self.assertTrue(
            any(item.event_type == "device.tuned" for item in self.service.events(limit=5000))
        )
        self.assertEqual(len(self.service.list_sweeps(session.id)), 0)

    def test_requires_running_session_and_rejects_invalid_tune(self) -> None:
        session = self.service.create_session(simulated_session_payload())
        with self.assertRaisesRegex(WorkerError, "Сначала запустите"):
            self.manager.start_session(session.id)
        self.service.start_session(session.id)
        with self.assertRaisesRegex(WorkerError, "не запущен"):
            self.manager.tune(
                session.id, session.streams[1].id, {"frequency_hz": 145_500_000}
            )

        payload = simulated_session_payload()
        del payload["streams"][0]["settings"]["adapter"]
        unsupported = self.service.create_session(payload)
        self.service.start_session(unsupported.id)
        with self.assertRaises(UnsupportedAdapterError):
            self.manager.start_stream(unsupported.id, unsupported.streams[0].id)


class PersistentWorkerTests(unittest.TestCase):
    def test_restart_restores_measurements_but_not_runtime_thread_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "workers.sqlite")
            service = SessionService(store=SQLiteSessionStore(path))
            session = service.create_session(simulated_session_payload())
            service.start_session(session.id)
            service.set_stream_state(session.id, session.streams[0].id, DeviceState.RUNNING)
            service.ingest_observation(
                ARDV1Simulator(session.streams[1]).poll()
            )
            service.close()

            restored = SessionService(store=SQLiteSessionStore(path))
            try:
                session_after = restored.get_session(session.id)
                self.assertEqual(session_after.state.value, "running")
                self.assertTrue(
                    all(item.state is DeviceState.DISCONNECTED for item in session_after.streams)
                )
                self.assertEqual(len(restored.list_observations(session.id)), 1)
                manager = DeviceWorkerManager(restored)
                self.assertEqual(manager.active_count, 0)
                self.assertTrue(
                    all(not item["alive"] for item in manager.list_workers(session.id))
                )
            finally:
                restored.close()


class WorkerHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temporary.name) / "worker-http.sqlite")
        self.server = None
        self.thread = None
        self.start_server()

    def start_server(self) -> None:
        self.server = create_server(
            host="127.0.0.1", port=0, session_database=self.database
        )
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def stop_server(self) -> None:
        if self.server is not None:
            self.server.worker_manager.close()
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.server is not None:
            self.server.session_service.close()
        self.server = None
        self.thread = None

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

    def test_http_continuous_two_device_session_tune_isolation_and_restart(self) -> None:
        status, capabilities = self.request("GET", "/api/v1/capabilities")
        self.assertEqual(status, 200)
        features = capabilities["data"]["features"]
        self.assertTrue(features["simulated_device_workers"])
        self.assertTrue(features["continuous_simulated_monitoring"])
        self.assertTrue(features["hardware_control"])
        self.assertTrue(features["hardware_validation_not_run"])

        status, created = self.request("POST", "/api/v1/sessions", simulated_session_payload())
        self.assertEqual(status, 201)
        session = created["data"]
        session_id = session["id"]
        tinysa_id, ardv1_id = [item["id"] for item in session["streams"]]
        status, _ = self.request("POST", f"/api/v1/sessions/{session_id}/start", {})
        self.assertEqual(status, 200)
        status, started = self.request(
            "POST", f"/api/v1/sessions/{session_id}/workers/start", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(started["data"]), 2)

        def measurements_ready() -> bool:
            _, sweeps = self.request("GET", f"/api/v1/sessions/{session_id}/sweeps")
            _, observations = self.request(
                "GET", f"/api/v1/sessions/{session_id}/observations"
            )
            return len(sweeps["data"]) >= 3 and len(observations["data"]) >= 3

        wait_until(measurements_ready, "HTTP API должен показать измерения обоих приборов")

        status, tuned = self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/streams/{ardv1_id}/tune",
            {"frequency_hz": 433_075_000, "mode": "DMR"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(tuned["data"]["tuned"]["frequency_hz"], 433_075_000)

        status, stopped = self.request(
            "POST", f"/api/v1/sessions/{session_id}/streams/{tinysa_id}/stop", {}
        )
        self.assertEqual(status, 200)
        self.assertFalse(stopped["data"]["alive"])
        _, sweeps = self.request("GET", f"/api/v1/sessions/{session_id}/sweeps")
        _, observations_before = self.request(
            "GET", f"/api/v1/sessions/{session_id}/observations"
        )
        time.sleep(0.08)
        _, sweeps_after = self.request("GET", f"/api/v1/sessions/{session_id}/sweeps")
        _, observations_after = self.request(
            "GET", f"/api/v1/sessions/{session_id}/observations"
        )
        self.assertEqual(len(sweeps_after["data"]), len(sweeps["data"]))
        self.assertGreater(len(observations_after["data"]), len(observations_before["data"]))

        status, stopped_session = self.request(
            "POST", f"/api/v1/sessions/{session_id}/stop", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(stopped_session["data"]["state"], "stopped")
        self.stop_server()
        self.start_server()

        status, restored = self.request("GET", f"/api/v1/sessions/{session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(restored["data"]["state"], "stopped")
        self.assertTrue(
            all(item["state"] == "disconnected" for item in restored["data"]["streams"])
        )
        status, workers = self.request("GET", f"/api/v1/sessions/{session_id}/workers")
        self.assertEqual(status, 200)
        self.assertTrue(all(not item["alive"] for item in workers["data"]))
        status, observations = self.request(
            "GET", f"/api/v1/sessions/{session_id}/observations?limit=1000"
        )
        self.assertEqual(status, 200)
        self.assertTrue(
            any(
                item["frequency_hz"] == 433_075_000 and item["decoded_mode"] == "DMR"
                for item in observations["data"]
            )
        )

    def test_http_worker_errors_are_structured(self) -> None:
        status, created = self.request("POST", "/api/v1/sessions", simulated_session_payload())
        session_id = created["data"]["id"]
        status, error = self.request(
            "POST", f"/api/v1/sessions/{session_id}/workers/start", {}
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["code"], "worker_error")

        self.request("POST", f"/api/v1/sessions/{session_id}/start", {})
        status, error = self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/streams/missing/start",
            {},
        )
        self.assertEqual(status, 400)
        self.assertIn("не принадлежит", error["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
