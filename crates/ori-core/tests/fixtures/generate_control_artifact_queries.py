"""Original artifact-dispatch number/error oracle; memory only, no workers."""

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from radioinspector.session_service import SessionService
from radioinspector.tinysa.api import InstrumentOperations, dispatch


class Handler:
    headers = {}

    def _send_json(self, value, status=200):
        self.result = {"status": status, "body": value}

    def _send_api_error(self, status, code, message):
        self.result = {"status": status, "error": {"code": code, "message": message}}


def main():
    handler = Handler()
    handler.sessions = SessionService()
    handler.instruments = InstrumentOperations(handler.sessions, None)
    session = handler.sessions.create_session(
        {
            "id": "artifact-query-fixture",
            "name": "Owned number fixture",
            "streams": [
                {
                    "id": "r",
                    "device": "Owned receiver",
                    "role": "receiver",
                    "level_unit": "s_meter_count",
                    "settings": {"adapter": "ardv1-sim"},
                }
            ],
        }
    )
    path = f"/api/v1/sessions/{session.id}/streams/r/instrument/artifacts"
    cases = []
    for key, value in [
        ("limit", "abc"),
        ("limit", "1__0"),
        ("limit", " +1_0 "),
        ("limit", "١٠"),
        ("limit", "１０"),
        ("limit", "\t+1\n"),
        ("limit", "\x1c1\x1f"),
        ("limit", "\u200b1"),
        ("limit", "0"),
        ("limit", "-1"),
        ("limit", "101"),
        ("limit", "9" * 100),
        ("offset", "-0"),
        ("offset", "-1"),
        ("offset", "10_000_000"),
        ("offset", "10_000_001"),
        ("offset", "xyz"),
    ]:
        assert dispatch(handler, "GET", path, query={key: [value]})
        result = handler.result
        cases.append(
            {
                "key": key,
                "value": value,
                "status": result["status"],
                **({"error": result["error"]} if "error" in result else {}),
            }
        )
    handler.instruments.close()
    source_paths = ["radioinspector/tinysa/api.py", "radioinspector/session_service.py"]
    target = Path(__file__).with_name("control-artifact-query-v1.json")
    target.write_text(
        json.dumps(
            {
                "source": "Original Python tinysa.api.dispatch + SessionService, memory only; no hardware",
                "source_sha256": {
                    path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                    for path in source_paths
                },
                "cases": cases,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
