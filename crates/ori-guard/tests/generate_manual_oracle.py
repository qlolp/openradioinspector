"""Run the original manual lease methods on owned simulated devices only."""
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from radioinspector.contracts import TimeInterval, utc_now
from radioinspector.device_workers import DeviceWorkerManager, TinySASimulator
from radioinspector.monitoring.service import MonitoringService
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_device_workers import simulated_session_payload, wait_until


def error(action):
    try:
        action()
    except (ValueError, RuntimeError) as exc:
        return str(exc)
    raise AssertionError("The original manual method unexpectedly accepted the request")


def oracle():
    with tempfile.TemporaryDirectory(prefix="ori-manual-oracle-") as directory:
        sessions = SessionService(store=SQLiteSessionStore(str(Path(directory) / "archive.sqlite")))
        workers = DeviceWorkerManager(sessions)
        service = MonitoringService(sessions, workers)
        try:
            session = sessions.create_session(simulated_session_payload(
                tinysa_settings={"noise_sigma_db": 0, "emitters": []}))
            project = service.create(dict(name="Manual oracle", site="Synthetic", session_id=session.id))
            adapter = TinySASimulator(session.streams[0])
            now = utc_now()
            for index in range(20):
                sample = adapter.poll()
                start = now - timedelta(seconds=80 - 4 * index)
                sample.interval = TimeInterval(start, start + timedelta(seconds=1))
                sessions.ingest_sweep(sample)
            service.create_baseline(project["id"], dict(name="Quiet", location="Fixture", confirm=True))
            project = service.arm(project["id"], {})
            wait_until(lambda: workers.active_count == 2, "owned simulator workers")
            identifier = project["id"]
            invalid_role = error(lambda: service.manual_acquire(identifier, {"role": "foreign"}))
            lease = service.manual_acquire(identifier, {"role": "receiver"})
            token = lease["token"]
            acquired = dict(lease, token="__TOKEN__")
            renewed = service.manual_renew(identifier, {"token": token})
            invalid_renew = error(lambda: service.manual_renew(identifier, {"token": token, "extra": 1}))
            foreign = error(lambda: service.manual_renew(identifier, {"token": "foreign"}))
            with service.lock:
                service.runtime[identifier]["manual"][project["receiver_stream_id"]]["expires"] = 0
            expired = error(lambda: service.manual_renew(identifier, {"token": token}))
            returned = service.manual_return(identifier, {"token": token})
            analyzer = service.manual_acquire(identifier, {"role": "analyzer"})
            statuses = workers.list_workers(project["active_session_id"])
            paused = dict(role=analyzer["role"], expires_seconds=analyzer["expires_seconds"],
                          coverage=service.project(identifier)["coverage"],
                          analyzer_alive=next(w["alive"] for w in statuses if w["stream_id"] == project["analyzer_stream_id"]),
                          receiver_alive=next(w["alive"] for w in statuses if w["stream_id"] == project["receiver_stream_id"]))
            return dict(synthetic=True, hardware_validation="not_run", acquired=acquired,
                        renewed=renewed, returned=returned, paused=paused,
                        errors=dict(invalid_role=invalid_role, invalid_renew=invalid_renew,
                                    foreign=foreign, expired=expired))
        finally:
            service.close()
            workers.close()
            sessions.close()


if __name__ == "__main__":
    print(json.dumps(oracle(), ensure_ascii=False, allow_nan=False))
