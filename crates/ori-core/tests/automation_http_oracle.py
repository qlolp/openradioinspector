"""Record actual legacy automation HTTP defaults without opening any device."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import faulthandler
import hashlib
import http.client
import json
import os
import platform
from pathlib import Path
import tempfile
import threading
import time

def capture(diagnostics=None) -> dict:
    def mark(phase: str) -> None:
        if diagnostics is not None:
            diagnostics.write(json.dumps({
                "pid": os.getpid(), "phase": phase,
                "utc": datetime.now(timezone.utc).isoformat(),
                "monotonic": time.monotonic(), "process_cpu": time.process_time(),
                "threads": threading.active_count(),
            }) + "\n")
            diagnostics.flush()

    mark("before_service_imports")
    from radioinspector.session_service import SessionService
    from radioinspector.session_store import SQLiteSessionStore
    from radioinspector.web.server import create_server
    mark("after_service_imports")
    with tempfile.TemporaryDirectory() as directory:
        sessions = SessionService(
            store=SQLiteSessionStore(str(Path(directory) / "sessions.sqlite3"))
        )
        session = sessions.create_session(
            {
                "id": "automation-http-oracle",
                "name": "Owned automation HTTP oracle",
                "streams": [
                    {
                        "id": "spectrum",
                        "device": "Owned synthetic analyzer",
                        "role": "spectrum_analyzer",
                        "level_unit": "dBm",
                        "settings": {"adapter": "tinysa-sim"},
                    },
                    {
                        "id": "receiver",
                        "device": "Owned synthetic receiver",
                        "role": "receiver",
                        "level_unit": "s_meter_count",
                        "settings": {"adapter": "ardv1-sim"},
                    },
                ],
            }
        )
        server = create_server(host="127.0.0.1", port=0, sessions=sessions)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        mark("server_started")

        def request(method: str, path: str, payload: dict | None = None) -> dict:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=5
            )
            try:
                body = None if payload is None else json.dumps(payload).encode()
                headers = {} if body is None else {"Content-Type": "application/json"}
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                return {"status": response.status, "body": json.loads(response.read())}
            finally:
                connection.close()

        try:
            base = f"/api/v1/sessions/{session.id}/automatic-"
            defaults = {
                kind: request("GET", base + kind)
                for kind in ("detection", "handoff", "completion")
            }
            errors = {
                kind: request("POST", base + kind, {"unknown": True})
                for kind in defaults
            }
            mark("defaults_and_errors_recorded")
            draft_policy = request(
                "POST", base + "detection",
                {"enabled": True, "interval_seconds": 0.1, "threshold_dbm": -60.0},
            )
            assert draft_policy["status"] == 200, draft_policy
            assert sessions.get_session(session.id).state.value == "draft"
            assert sessions.list_candidates(session.id) == []
            unrelated = {
                kind: request("GET", base + kind) for kind in ("handoff", "completion")
            }
            mark("draft_opt_in_and_unrelated_statuses_recorded")
            started = request("POST", f"/api/v1/sessions/{session.id}/start", {})
            assert started["status"] == 200, started
            mark("session_started")
            sweep = {
                "id": "after-draft-opt-in",
                "session_id": session.id,
                "stream_id": "spectrum",
                "interval": {
                    "started_at": "2026-11-11T12:00:00.123456Z",
                    "ended_at": "2026-11-11T12:00:01.123456Z",
                },
                "frequencies_hz": [100000000, 100100000, 100200000, 100300000,
                                   100400000, 100500000, 100600000],
                "levels": [-90.0, -55.0, -45.0, -80.0, -50.0, -48.0, -90.0],
                "level_unit": "dBm",
                "settings": {"rbw_hz": 10000},
            }
            ingested = request("POST", f"/api/v1/sessions/{session.id}/sweeps", sweep)
            assert ingested["status"] == 201, ingested
            deadline = time.monotonic() + 5
            while True:
                candidates = request("GET", f"/api/v1/sessions/{session.id}/candidates")
                assert candidates["status"] == 200, candidates
                if len(candidates["body"]["data"]) == 2:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError("Actual detector did not activate after Draft opt-in/start")
                time.sleep(0.01)
            mark("candidates_created")
            selected = [
                {key: item[key] for key in (
                    "source_stream_id", "interval", "center_frequency_hz", "bandwidth_hz",
                    "peak_level", "score", "level_unit", "state",
                )}
                for item in candidates["body"]["data"]
            ]
            selected.sort(key=lambda item: item["center_frequency_hz"])
            assert request("POST", base + "detection", {"enabled": False})["status"] == 200
            assert all(
                not item["alive"]
                for item in server.worker_manager.list_workers(session.id)
            )
            source_root = Path(__file__).resolve().parents[3]
            sources = {}
            for name in (
                "radioinspector/automatic_detection.py",
                "radioinspector/automatic_handoff.py",
                "radioinspector/automatic_completion.py",
                "radioinspector/web/server.py",
            ):
                sources[name] = hashlib.sha256((source_root / name).read_bytes()).hexdigest()
            return {
                "defaults": defaults, "errors": errors,
                "after_detection_opt_in": unrelated,
                "draft_opt_in": {"sweep": sweep, "candidates": selected},
                "source_sha256": sources,
                "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation()},
            }
        finally:
            mark("before_cleanup")
            server.shutdown()
            thread.join(5)
            server.server_close()
            sessions.close()
            assert not thread.is_alive()
            mark("cleanup_completed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--diagnostics", type=Path)
    args = parser.parse_args()
    if args.diagnostics is not None:
        args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
        with args.diagnostics.open("w", encoding="utf-8") as diagnostics:
            faulthandler.enable(file=diagnostics)
            faulthandler.dump_traceback_later(10, repeat=True, file=diagnostics)
            try:
                result = capture(diagnostics)
            finally:
                faulthandler.cancel_dump_traceback_later()
                faulthandler.disable()
    else:
        result = capture()
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
