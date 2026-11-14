"""Generate legacy automation oracles from owned actual Python SQLite services.

No ports, network, audio, webhooks, or background threads are started. The
scientific regions are produced by the existing Python authority, not Rust.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

from radioinspector import contracts, session_service
from radioinspector.automatic_detection import AutomaticCandidateDetector, detect_regions
from radioinspector.automatic_handoff import AutomaticHandoffScheduler
from radioinspector.automatic_completion import AutomaticInvestigationCompletion
from radioinspector.contracts import Sweep, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore

ROOT = Path(__file__).resolve().parents[2]
BASE = datetime(2026, 11, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)
SID, SPECTRUM, RECEIVER = "session-é", "spectrum/e\u0301", "receiver/é"


def interval(at=BASE):
    return {"started_at": to_primitive(at), "ended_at": to_primitive(at + timedelta(microseconds=1))}


def sweep(identifier, *, at=BASE):
    return {"id": identifier, "session_id": SID, "stream_id": SPECTRUM,
            "interval": interval(at), "frequencies_hz": [100000001 + n * 100000 for n in range(7)],
            "levels": [-100.125, -0.0, -1.0000000000000002, -90.0, -55.0, -54.0, -100.0],
            "level_unit": "dBm", "settings": {"rbw_hz": 10000, "synthetic": True, "opaque": {"é": 1, "e\u0301": 2}}}


def candidate(identifier, frequency=100100001):
    return {"id": identifier, "source_stream_id": SPECTRUM, "interval": interval(),
            "center_frequency_hz": frequency, "bandwidth_hz": 100000,
            "peak_level": -0.0, "score": 60.0, "level_unit": "dBm"}


def observation(identifier, at, *, mode="FM", frequency=100100001, stream=RECEIVER):
    return {"id": identifier, "session_id": SID, "stream_id": stream,
            "interval": interval(at), "frequency_hz": frequency, "mode": mode,
            "level": -0.0, "level_unit": "s_meter_count",
            "metadata": {"synthetic": True, "é": -0.0, "e\u0301": -1.0000000000000002}}


class Oracle:
    def __init__(self, path):
        self.at, self.tick, self.counter = BASE, 0.0, 0
        self.effect, self.delay = "ok", 0
        self.service = SessionService(store=SQLiteSessionStore(str(path)), clock=lambda: self.at)
        created = self.service.create_session({"id": SID, "name": "Synthetic legacy automation",
            "metadata": {"é": 1, "e\u0301": 2}, "streams": [
                {"id": SPECTRUM, "device": "synthetic", "role": "spectrum_analyzer", "level_unit": "dBm"},
                {"id": RECEIVER, "device": "synthetic", "role": "receiver", "level_unit": "s_meter_count"},
                {"id": "receiver/e\u0301", "device": "synthetic", "role": "receiver", "level_unit": "s_meter_count"}]})
        self.service.start_session(created.id)
        self.seed = {"sessions": [to_primitive(self.service.get_session(SID))]}
        self.actions = []
        self.reset()

    def reset(self):
        if hasattr(self, "policies"):
            for policy in self.policies.values():
                policy.close()
        common = dict(clock=lambda: self.at, monotonic=lambda: self.tick, start_background=False)
        self.policies = {
            "detection": AutomaticCandidateDetector(self.service, **common),
            "handoff": AutomaticHandoffScheduler(self.service, tune=self.tune, **common),
            "completion": AutomaticInvestigationCompletion(self.service, **common)}

    def tune(self, *_args):
        self.at += timedelta(seconds=self.delay)
        self.tick += self.delay
        if self.effect != "ok":
            raise RuntimeError(self.effect)
        return {}

    def identifier(self):
        self.counter += 1
        return f"generated-{self.counter:04d}"

    def action(self, kind, method, *args, effect="ok", delay=0):
        self.effect, self.delay = effect, delay
        before = self.service.health()["last_event_id"]
        step = {"kind": kind, "method": method, "args": args,
                "at": to_primitive(self.at), "monotonic": self.tick,
                "effect": effect, "tune_delay_seconds": delay}
        try:
            if method == "restart":
                self.reset()
                result = {name: policy.status(SID) for name, policy in self.policies.items()}
            elif kind == "domain":
                result = getattr(self.service, method)(*args)
            else:
                result = getattr(self.policies[kind], method)(SID, *args)
            step["result"] = to_primitive(result)
        except (KeyError, ValueError, TypeError) as error:
            step["error"] = {"http_status": 400, "api_code": "invalid_request", "message": str(error)}
        step["events"] = [dict(to_primitive(event), sequence=event.sequence - before)
                          for event in self.service.events(after=before)]
        self.actions.append(step)

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)
        self.tick += seconds

    def enqueue(self, identifier, priority=0, *, receiver=RECEIVER, frequency=100100001):
        self.action("domain", "create_candidate", SID, candidate("candidate-" + identifier, frequency))
        self.action("domain", "enqueue_investigation", SID, "candidate-" + identifier,
                    {"id": identifier, "target_stream_id": receiver, "priority": priority, "mode": " fm "})


def combined(o):
    for kind in o.policies:
        o.action(kind, "status")
        o.action(kind, "run_once")
    o.action("domain", "ingest_sweep", sweep("old-é"))
    o.action("detection", "configure", {"enabled": True, "threshold_dbm": -60.0})
    o.action("detection", "run_once")
    o.action("domain", "ingest_sweep", sweep("é"))
    o.action("domain", "ingest_sweep", sweep("e\u0301", at=BASE - timedelta(seconds=1)))
    o.action("detection", "run_once")
    o.action("detection", "status")
    o.action("domain", "enqueue_investigation", SID, "generated-0001",
             {"id": "job-é", "target_stream_id": RECEIVER, "priority": 9, "mode": " fm "})
    o.action("handoff", "configure", {"enabled": True, "minimum_priority": 5})
    o.action("handoff", "run_once", delay=10)
    o.action("completion", "configure", {"enabled": True, "minimum_listening_seconds": 10.0, "minimum_observations": 2})
    for item in [observation("before", BASE - timedelta(microseconds=1)),
                 observation("wrong-mode", BASE + timedelta(seconds=11), mode="AM"),
                 observation("wrong-frequency", BASE + timedelta(seconds=11), frequency=100100002),
                 observation("wrong-stream", BASE + timedelta(seconds=11), stream="receiver/e\u0301"),
                 observation("é", BASE + timedelta(seconds=11))]:
        o.action("domain", "ingest_observation", item)
    o.action("completion", "run_once")
    o.advance(10)
    o.action("completion", "run_once")
    o.action("domain", "ingest_observation", observation("e\u0301", BASE + timedelta(seconds=12)))
    o.action("completion", "run_once")
    o.action("completion", "status")
    o.action("detection", "configure", {"enabled": False})
    o.action("domain", "ingest_sweep", sweep("disabled"))
    o.action("detection", "configure", {"enabled": True})
    o.action("detection", "run_once")
    o.action("all", "restart")


def validation(o):
    for kind in o.policies:
        for payload in [{"enabled": 1}, {"enable": True}, {"interval_seconds": False},
                        {"interval_seconds": "0.01"}, {"interval_seconds": "NaN"},
                        {"interval_seconds": "1.25"}, {"interval_seconds": "١_٢.٥"},
                        {"interval_seconds": "1__0"}, {"interval_seconds": " +Infinity "}]:
            o.action(kind, "configure", payload)
        field = "stream_id" if kind == "detection" else "receiver_stream_id"
        o.action(kind, "configure", {field: "missing"})
        o.action(kind, "configure", {field: RECEIVER if kind == "detection" else SPECTRUM})
        o.action(kind, "status")
    for payload in [{"dedup_bandwidth_hz": 1.5}, {"cooldown_seconds": -1},
                    {"max_candidates_per_sweep": 0}, {"threshold_dbm": -301}]:
        o.action("detection", "configure", payload)
    for payload in [{"minimum_priority": -1}, {"max_consecutive_failures": 101}, {"retry_delay_seconds": 0}]:
        o.action("handoff", "configure", payload)
    for payload in [{"minimum_observations": 0}, {"minimum_listening_seconds": -1}]:
        o.action("completion", "configure", payload)


def failures(o):
    o.enqueue("low", 2)
    o.enqueue("é", 9)
    o.enqueue("e\u0301", 9)
    o.action("handoff", "configure", {"enabled": True, "minimum_priority": 5,
             "max_consecutive_failures": 2, "retry_delay_seconds": 60.0})
    o.action("handoff", "run_once", effect="Стенд: receiver недоступен")
    o.action("handoff", "run_once", effect="Стенд: receiver недоступен")
    o.action("handoff", "run_once")
    o.action("handoff", "status")
    o.action("handoff", "configure", {"enabled": True, "minimum_priority": 0})
    o.action("handoff", "run_once")
    o.action("domain", "stop_session", SID)
    for kind in o.policies:
        o.action(kind, "configure", {"enabled": True})
        o.action(kind, "run_once")


def main():
    cases = []
    with tempfile.TemporaryDirectory(prefix="ori-automation-oracle-") as temporary:
        for name, execute in [("combined", combined), ("validation", validation), ("failures", failures)]:
            oracle = Oracle(Path(temporary) / (name + ".sqlite"))
            with patch.object(contracts, "new_id", oracle.identifier), patch.object(session_service, "new_id", oracle.identifier):
                execute(oracle)
            cases.append({"name": name, "seed": oracle.seed, "actions": oracle.actions})
            for policy in oracle.policies.values():
                policy.close()
            oracle.service.close()
    samples = [sweep("region-é"), {**sweep("region-e\u0301"), "levels": [-80.0, -60.0, -60.0, -70.0, -0.0, -0.0, -80.0]}]
    regions = [{"sweep": value, "threshold_dbm": -60.0, "maximum": 10,
                "regions": detect_regions(Sweep.from_dict(value), threshold_dbm=-60.0, maximum=10)} for value in samples]
    sources = ["automatic_detection.py", "automatic_handoff.py", "automatic_completion.py", "session_service.py", "session_store.py", "contracts.py"]
    result = {"format": "ori-automation-python-sqlite-oracle", "format_version": 1,
              "runtime": {"python": sys.version, "sqlite": sqlite3.sqlite_version},
              "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "source_sha256": {name: hashlib.sha256((ROOT / "radioinspector" / name).read_bytes()).hexdigest() for name in sources},
              "regions": regions, "cases": cases}
    target = Path(__file__).with_name("automation-domain-v1.json")
    target.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
