"""Observe the existing Python outbox on owned SQLite and a pure fake transport."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from radioinspector.monitoring.operations import OperationalMonitoring
from radioinspector.monitoring.store import ProjectStore
from radioinspector.session_store import SQLiteSessionStore, _archive_time

STAMP = datetime(2026, 11, 11, tzinfo=timezone.utc).replace(microsecond=123456)
KEY = "public-synthetic-outbox-key"


class Reply:
    status = 204
    def __enter__(self): return self
    def __exit__(self, *_): return False


class Harness(OperationalMonitoring):
    def project(self, identifier): return self.store.get("project", identifier)


def snapshot(harness, identifier):
    row = harness.store.db.execute("SELECT * FROM monitoring_notifications WHERE id=?", (identifier,)).fetchone()
    if row is None: return None
    result = dict(row)
    result["payload_json"] = result["payload"]
    result["payload"] = json.loads(result["payload"])
    return result


class Transport:
    def __init__(self, harness, identifier):
        self.harness, self.identifier = harness, identifier
        self.fail, self.requests = False, []
    def open(self, request, timeout):
        self.requests.append({"claim": snapshot(self.harness, self.identifier), "url": request.full_url,
            "body": request.data.decode("utf-8"), "headers": dict(request.header_items()), "timeout": timeout})
        if self.fail: raise URLError("synthetic unavailable")
        return Reply()


def setup():
    harness = Harness()
    harness.store = ProjectStore(SQLiteSessionStore(":memory:"))
    harness.lock, harness.runtime = threading.RLock(), {}
    project = {"id": "project", "project_id": "project", "created_at": _archive_time(STAMP),
               "session_ids": [], "webhook_enabled": True}
    harness.store.put("project", project)
    harness.store.db.execute("INSERT INTO monitoring_webhooks VALUES (?,?,?)", ("project", "https://synthetic.invalid/notify", KEY))
    harness.store.db.commit()
    return harness, project


def emit():
    harness, project = setup()
    clock = [STAMP]
    try:
        with patch("radioinspector.monitoring.operations.utc_now", lambda: clock[0]):
            identifier = harness._notify(project, "owned-source", "new_signal", "warning", "Fixture π", None)
            original = snapshot(harness, identifier)
            duplicate = harness._notify(project, "owned-source", "new_signal", "warning", "changed duplicate", None)
            transport = Transport(harness, identifier)
            harness._delivery_opener = transport
            transport.fail = True
            first = harness.deliver_one(STAMP)
            failed = snapshot(harness, identifier)
            early = harness.deliver_one(STAMP + timedelta(seconds=9))
            transport.fail = False
            second = harness.deliver_one(STAMP + timedelta(seconds=10))
            sent = snapshot(harness, identifier)
            clock[0] = STAMP + timedelta(seconds=50)
            harness.acknowledge_alert("project", identifier, {})
            clock[0] = STAMP + timedelta(seconds=51)
            harness.acknowledge_alert("project", identifier, {})
            acknowledged = snapshot(harness, identifier)
            return {"synthetic": True, "hardware_validation": "not_run", "now": _archive_time(STAMP),
                "project": project, "url": "https://synthetic.invalid/notify", "secret": KEY,
                "identifier": identifier, "duplicate_identifier": duplicate, "original": original,
                "first_delivery": first, "retry": failed, "early_delivery": early, "second_delivery": second,
                "sent": sent, "acknowledged": acknowledged, "transport": transport.requests}
    finally:
        harness.store.store.close()


def main():
    output = Path(__file__).resolve().parent / "fixtures/outbox_python_oracle.json"
    output.write_text(json.dumps(emit(), ensure_ascii=False, allow_nan=False, indent=2) + "\n")


if __name__ == "__main__": main()
