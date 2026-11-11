"""Generate Rust session parity vectors from the actual Python SQLite service.

Run with the repository on PYTHONPATH and an existing core Python environment.
Only deterministic synthetic archives in TemporaryDirectory are written.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unicodedata
from unittest.mock import patch

from radioinspector import contracts, measurement_results, session_service
from radioinspector.contracts import to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore

ROOT = Path(__file__).resolve().parents[2]
BASE = datetime(2026, 11, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)


def interval(start=BASE, end=None):
    return {"started_at": to_primitive(start), "ended_at": to_primitive(end or start)}


def candidate(identifier="candidate-α", **changes):
    return {"id": identifier, "source_stream_id": "spectrum/α", "interval": interval(),
            "center_frequency_hz": 100000003, "bandwidth_hz": 25000,
            "peak_level": -0.0, "score": -1.0000000000000002, "level_unit": "dBm", **changes}


def sweep(identifier="sweep-α", **changes):
    return {"id": identifier, "session_id": "session-α", "stream_id": "spectrum/α",
            "interval": interval(), "frequencies_hz": [100000001, 100000003, 100000005],
            "levels": [-100.125, -0.0, -1.0000000000000002], "level_unit": "dBm",
            "settings": {"rbw_hz": 10000, "generator_output": False}, **changes}


def observation(identifier, *, at=BASE, mode="FM", frequency=100000003, stream="receiver+α"):
    return {"id": identifier, "session_id": "session-α", "stream_id": stream,
            "interval": interval(at, at + timedelta(microseconds=1)),
            "frequency_hz": frequency, "mode": mode, "level": -0.0,
            "level_unit": "s_meter_count", "metadata": {"synthetic": True}}


ERRORS = {
    "SessionNotFoundError": (404, "session_not_found"),
    "CandidateNotFoundError": (404, "candidate_not_found"),
    "InvestigationNotFoundError": (404, "investigation_not_found"),
    "SweepNotFoundError": (404, "sweep_not_found"),
    "ReferenceNameConflictError": (409, "reference_name_conflict"),
    "ReferenceLimitReachedError": (409, "reference_limit_reached"),
    "SignalListError": (409, "signal_list_error"),
    "ComparisonIncompatibleError": (409, "comparison_incompatible"),
}


class Oracle:
    def __init__(self, path):
        self.at = BASE
        self.counter = 0
        self.service = SessionService(store=SQLiteSessionStore(str(path)), clock=lambda: self.at)
        self.sessions = []
        self.sweeps = [sweep(), sweep("sweep-β")]
        self.observations = [
            observation("before-overlap", at=BASE - timedelta(microseconds=1)),
            observation("wrong-frequency", frequency=100000004),
            observation("wrong-mode", mode="AM"),
            observation("tie-\ue000"), observation("tie-\U00010000"),
            observation("after", at=BASE + timedelta(seconds=1)),
        ]
        for sid, suffix in [("session-α", "α"), ("session-foreign", "foreign")]:
            value = self.service.create_session({"id": sid, "name": "Synthetic session parity",
                "streams": [{"id": "spectrum/" + suffix, "device": "synthetic", "role": "spectrum_analyzer", "level_unit": "dBm"},
                            {"id": "receiver+" + suffix, "device": "synthetic", "role": "receiver", "level_unit": "s_meter_count"}]})
            self.service.start_session(value.id)
            self.sessions.append(to_primitive(self.service.get_session(value.id)))
        for value in self.sweeps:
            self.service.ingest_sweep(value)
        for value in self.observations:
            self.service.ingest_observation(value)
        self.seed = {"sessions": self.sessions, "sweeps": self.sweeps, "observations": self.observations}
        self.actions = []

    def identifier(self):
        self.counter += 1
        return f"generated-{self.counter:04d}"

    def action(self, method, *args, effect=None, **kwargs):
        before = self.service.health()["last_event_id"]
        value = {"method": method, "args": args, "kwargs": kwargs, "at": to_primitive(self.at)}
        if effect is not None:
            value["effect"] = effect
            def tune(*unused):
                if effect != "ok":
                    raise RuntimeError(effect)
                return {}
            kwargs = dict(kwargs, tune=tune)
        try:
            result = getattr(self.service, method)(*args, **kwargs)
            if method == "create_sweep_reference":
                result = {"reference": result[0], "created": result[1]}
            value["result"] = to_primitive(result)
        except (KeyError, ValueError, TypeError) as error:
            kind = type(error).__name__
            status, code = ERRORS.get(kind, (400, "invalid_request"))
            message = str(error.args[0]) if isinstance(error, KeyError) else str(error)
            if kind == "KeyError":
                code, message = "missing_field", "Отсутствует поле: " + str(error.args[0])
            value["error"] = {"http_status": status, "api_code": code, "message": message}
        value["events"] = [dict(to_primitive(event), sequence=event.sequence - before)
                           for event in self.service.events(after=before)]
        self.actions.append(value)


def lifecycle(o):
    sid = "session-α"
    o.action("create_candidate", "missing", candidate(state="resolved"))
    o.action("create_candidate", sid, candidate(observation_ids=["sweep-α"]))
    o.action("create_candidate", sid, candidate(observation_ids=["absent"]))
    o.action("get_candidate", "session-foreign", "candidate-α")
    o.action("enqueue_investigation", sid, "candidate-α", {"id": "job-α", "target_stream_id": "receiver+α", "mode": " fm ", "priority": 9})
    o.action("dismiss_candidate", sid, "candidate-α")
    o.action("start_next_investigation", sid, "job-α", effect="ok")
    o.action("matching_observation_ids", sid, "job-α")
    o.action("complete_investigation", sid, "job-α")
    o.action("cancel_investigation", sid, "job-α")
    o.action("create_candidate", sid, candidate("candidate-failure"))
    o.action("enqueue_investigation", sid, "candidate-failure", {"id": "job-failure", "target_stream_id": "receiver+α"})
    o.action("start_investigation", sid, "job-failure", effect="frequency applied; mode failed")
    o.action("enqueue_investigation", sid, "candidate-failure", {"id": "job-cancel", "target_stream_id": "receiver+α"})
    o.action("cancel_investigation", sid, "job-cancel")
    o.action("dismiss_candidate", sid, "candidate-failure")
    o.action("list_candidates", sid)
    o.action("list_investigations", sid)


def references(o):
    sid = "session-α"
    o.action("create_sweep_reference", sid, {"name": "  Cafe\u0301  ", "sweep_id": "sweep-α"})
    o.action("create_sweep_reference", sid, {"name": "Café", "sweep_id": "sweep-α"})
    o.action("create_sweep_reference", sid, {"name": "Café", "sweep_id": "sweep-β"})
    for name in ["x\n", "x\u200d", "x\ue000", "x\u0378", "😀" * 81]:
        o.action("create_sweep_reference", "missing", {"name": name, "sweep_id": "absent"})
    for index in range(99):
        o.action("create_sweep_reference", sid, {"name": f"label-{index}", "sweep_id": "sweep-α"})
    o.action("create_sweep_reference", sid, {"name": "Café", "sweep_id": "sweep-α"})
    o.action("create_sweep_reference", sid, {"name": "Café", "sweep_id": "sweep-β"})
    o.action("create_sweep_reference", sid, {"name": "extra", "sweep_id": "sweep-α"})
    o.action("list_sweep_references", sid)


def priorities(o):
    sid = "session-α"
    for name, priority in [("runtime", 99), ("closed", 999), ("low", 1), ("tie-\U00010000", 9), ("tie-\ue000", 9)]:
        if name in ("closed", "low", "tie-\U00010000"):
            o.at += timedelta(seconds=1)
        o.action("create_candidate", sid, candidate("candidate-" + name))
        o.action("enqueue_investigation", sid, "candidate-" + name,
                 {"id": name, "target_stream_id": "receiver+α", "priority": priority})
        if name == "runtime":
            o.action("start_investigation", sid, name, effect="ok")
        if name == "closed":
            o.action("fail_investigation", sid, name, " synthetic failure ")
    o.action("list_investigations", sid)
    o.action("next_investigation", sid)


def catalog(o):
    sid = "session-α"
    o.action("measurement_catalog")
    for methodology, spec in sorted(measurement_results.METHODOLOGIES.items()):
        values = {key: (-0.0 if index == 0 else -1.0000000000000002) for index, key in enumerate(spec["fields"])}
        o.action("save_measurement_result", sid, {"stream_id": "receiver+α", "methodology": methodology,
            "result_code": "norm", "frequency_hz": 9007199254740991, "interval": interval(), "values": values})
    o.action("list_measurement_results", sid)
    o.action("save_measurement_result", "missing", {"stream_id": "absent", "methodology": "invalid"})


def signals(o):
    sid = "session-α"
    o.action("create_candidate", sid, candidate())
    artifact = o.service.signal_list_export(sid, "  Signals  ")["list"]
    o.action("signal_list_export", sid, "  Signals  ")
    o.action("signal_list_import", sid, {"stream_id": "spectrum/α", "list_json": json.dumps(artifact, ensure_ascii=False)})
    o.action("signal_list_import", sid, {"stream_id": "receiver+α", "list_json": json.dumps(artifact, ensure_ascii=False)})
    o.action("signal_list_import", "missing", {"stream_id": "absent", "list_json": '{"format":1,"format":2}'})
    o.action("list_candidates", sid)


def main():
    cases = []
    with tempfile.TemporaryDirectory(prefix="ori-session-oracle-") as temporary:
        for name, execute in [("lifecycle", lifecycle), ("priorities", priorities), ("references", references), ("catalog", catalog), ("signals", signals)]:
            oracle = Oracle(Path(temporary) / (name + ".sqlite"))
            with patch.object(contracts, "new_id", oracle.identifier), patch.object(session_service, "new_id", oracle.identifier), patch.object(measurement_results, "utc_now", lambda: oracle.at):
                execute(oracle)
            cases.append({"name": name, "seed": oracle.seed, "actions": oracle.actions})
            oracle.service.close()
    sources = ["session_service.py", "session_store.py", "contracts.py", "signal_list.py", "sweep_references.py", "measurement_results.py", "spectral_mask.py", "web/server.py"]
    sources.extend(str(path.relative_to(ROOT / "radioinspector")) for path in sorted((ROOT / "radioinspector").glob("*candidate*precision*.py")))
    result = {"format": "ori-session-python-sqlite-oracle", "format_version": 1,
        "runtime": {"python": sys.version, "sqlite": sqlite3.sqlite_version, "unicode": unicodedata.unidata_version},
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": {name: hashlib.sha256((ROOT / "radioinspector" / name).read_bytes()).hexdigest() for name in sources}, "cases": cases}
    target = Path(__file__).with_name("session-domain-v1.json")
    target.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
