"""Actual Python TaskBook oracle; all paths and files belong to TemporaryDirectory.

Write an explicit output with --output. Without it, emit JSON to stdout, allowing
reproduction checks without replacing the preserved pre-Rust golden.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from radioinspector.contracts import to_primitive
from radioinspector.task_scheduler import TaskScheduler, TaskNotFoundError
from radioinspector.tasks import ControlTask, FrequencyControlProfile, ReferenceBook, TaskBook

ROOT = Path(__file__).resolve().parents[2]
BASE = datetime(2026, 11, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)


def scrub(value, root):
    if isinstance(value, str):
        return value.replace(str(root), "$TMP")
    if isinstance(value, (list, tuple)):
        return [scrub(item, root) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item, root) for key, item in value.items()}
    return value


class Oracle:
    def __init__(self, root, seed=None, failures=None):
        self.root = root
        self.root.mkdir(mode=0o700)
        self.path = root / "existing-book.json"
        self.input = root / "scan-данные.json"
        self.input.write_text("{}\n", encoding="utf-8")
        self.at = BASE
        self.calls = []
        self.failures = failures or {}
        self.actions = []
        book = TaskBook(self.path)
        for raw in seed or []:
            book.add(ControlTask.from_dict(raw))
        if seed is not None:
            book.save()
        self.seed = None if seed is None else json.loads(self.path.read_text(encoding="utf-8"))
        self.scheduler = TaskScheduler(book, clock=lambda: self.at,
            on_start=lambda task: self.callback("start", task),
            on_complete=lambda task: self.callback("complete", task), interval_seconds=60)
        self.initial = self.snapshot()

    def callback(self, phase, task):
        self.calls.append({"phase": phase, "task": task.as_dict()})
        if (phase, task.name) in self.failures:
            raise RuntimeError(self.failures[phase, task.name])

    def payload(self, name="api/task", **changes):
        return {"name": name, "task_file": str(self.input),
            "start_at": "2026-11-11T14:00:00.123456+02:00", "duration_s": 10,
            "operator": "Оператор е\u0301", "equipment": "synthetic, no hardware", **changes}

    def snapshot(self):
        return {"status": self.scheduler.status(),
            "disk": json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None,
            "calls": copy.deepcopy(self.calls)}

    def action(self, method, *args, at=None):
        action = {"method": method, "args": args, "at": to_primitive(at or self.at)}
        try:
            if method == "run_once":
                result = self.scheduler.run_once(at or self.at)
            elif method == "configure":
                payload = args[0]
                if not isinstance(payload, dict) or set(payload) != {"enabled"}:
                    raise ValueError("Разрешено только поле enabled")
                result = self.scheduler.configure(payload["enabled"])
            else:
                result = getattr(self.scheduler, method)(*args)
            action["result"] = result
        except (TypeError, ValueError, RuntimeError, TaskNotFoundError) as error:
            missing = isinstance(error, TaskNotFoundError)
            # The live HTTP router maps validation/not-found, but not internal
            # after-close RuntimeError. Preserve its message without inventing
            # a successful HTTP response or an authority status code.
            internal = isinstance(error, RuntimeError)
            action["error"] = {"http_status": None if internal else (404 if missing else 400),
                "api_code": None if internal else ("task_not_found" if missing else "invalid_request"),
                "message": str(error)}
        action.update(self.snapshot())
        self.actions.append(action)

    def case(self, name):
        result = {"name": name, "seed": self.seed, "failures": [
            {"phase": phase, "name": name, "message": message}
            for (phase, name), message in self.failures.items()],
            "initial": self.initial, "actions": self.actions}
        self.scheduler.close()
        return scrub(result, self.root)


def validation(root):
    o = Oracle(root)
    for payload in [None, [], 5, {}, {"zzz": 1, "aaa": 2},
        o.payload(name=" \t"), o.payload(name=5), o.payload(start_at=5),
        o.payload(duration_s=True), o.payload(duration_s="1"), o.payload(duration_s=0),
        o.payload(duration_s=-1), o.payload(task_file=None),
        o.payload(panorama_file=str(o.input)), o.payload(task_file=""),
        o.payload(task_file="", panorama_file=str(o.input)),
        o.payload(task_file=10), o.payload(purpose=1), o.payload(equipment=None),
        o.payload(start_at="not-a-date"), o.payload(start_at="2026-11-11T12:00:00"),
        o.payload(start_at=""), o.payload(start_at="2026-11-11T99:00:00Z")]:
        o.action("create_task", payload)
    for payload in [None, {}, {"extra": 1}, {"enabled": 1}, {"enabled": None}, {"enabled": "true"}]:
        o.action("configure", payload)
    o.action("create_task", o.payload(name="  api/task  "))
    o.action("create_task", o.payload(name="api/task"))
    o.action("create_task", o.payload(name="е\u0301", start_at="2026-11-11T12:00:00.0000009Z"))
    o.action("create_task", o.payload(name="é", panorama_file=str(o.input), task_file=None))
    o.action("list_tasks")
    o.action("cancel_task", "api/task")
    o.action("cancel_task", "api/task")
    o.action("cancel_task", "missing")
    o.action("status")
    o.action("close")
    o.action("run_once")
    o.action("create_task", o.payload(name="closed"))
    o.action("configure", {"enabled": True})
    o.action("list_tasks")
    return o.case("validation")


def lifecycle(root):
    failures = {("start", "partial-start"): "frequency applied; mode failed",
        ("complete", "complete-failure"): "completion effect failed"}
    o = Oracle(root, failures=failures)
    for name, change in [("missing", {"task_file":str(root / "missing.json")}),
        ("valid", {}), ("partial-start", {}), ("complete-failure", {}),
        ("future", {"start_at":to_primitive(BASE + timedelta(seconds=100))})]:
        o.action("create_task", o.payload(name, **change))
    o.action("run_once", at=BASE + timedelta(seconds=20))
    o.action("configure", {"enabled": True})
    o.action("run_once", at=BASE - timedelta(microseconds=1))
    o.action("run_once", at=BASE)
    o.action("cancel_task", "valid")
    o.action("run_once", at=BASE + timedelta(seconds=9))
    o.action("configure", {"enabled": False})
    o.action("run_once", at=BASE + timedelta(seconds=1000))
    o.action("list_tasks")
    o.action("configure", {"enabled": True})
    o.action("configure", {"enabled": True})
    o.action("run_once", at=BASE + timedelta(seconds=10))
    o.action("run_once", at=BASE + timedelta(seconds=200))
    o.action("close")
    o.action("status")
    return o.case("lifecycle")


def recovery(root):
    task = ControlTask(name="running/е\u0301", task_file=str(root / "scan-данные.json"), start_at=BASE,
        duration_s=10.000000000000002, status="Выполняется", completed_cycles=4,
        task_type_id=1, purpose_id=2, rkp_id=3, department_id=4, group_pa_id=5, basis_id=6, carrier_id=7,
        created_at=BASE - timedelta(seconds=1), prepared_at=BASE, real_started_at=BASE,
        real_stopped_at=BASE, break_at=BASE, break_reason="обед", created_by_operator="Создатель",
        prepared_by_operator="Подготовил", device_names=["synthetic"], device_ids=["id/е\u0301"],
        observers=["Петров"], extra_info="сектор 4", result_code="0", result_text="без измерений",
        process_position="позиция12", mobility="выездное", next_times=[BASE], attachments=["панорама.pan"],
        protocol_file="протокол.doc", sleep_interval_s=-0.0,
        profiles=[FrequencyControlProfile(dynamic_threshold_db=-0.0)])
    queued = ControlTask(name="queued", panorama_file=str(root / "scan-данные.json"),
        start_at=BASE, duration_s=5, status="В очереди на выполнение")
    o = Oracle(root, seed=[task.as_dict(), queued.as_dict()])
    o.action("list_tasks")
    o.action("run_once", at=BASE + timedelta(seconds=20))
    o.action("configure", {"enabled":True})
    o.action("run_once", at=BASE + timedelta(seconds=20))
    o.action("run_once", at=BASE + timedelta(seconds=21))
    o.action("close")
    return o.case("crash-recovery-full-fields")


def references():
    before = {"purpose": [{"reference_id": 99, "name":"е\u0301", "code":"a"},
        {"reference_id": 2,"name":"é"}], "custom": [{"name":"Оператор", "code":"не нормализовать"}]}
    book = ReferenceBook.from_dict(before)
    added = book.add("purpose", "third", "x").as_dict()
    return {"input": before, "added":added, "book":book.as_dict(),
        "list": [item.as_dict() for item in book.list("purpose")],
        "found":book.get("purpose", 2).as_dict(),"missing":book.get("absent", 1),
        "empty_name":book.name_of("purpose", 0),"name":book.name_of("purpose", 1)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ori-tasks-oracle-") as temporary:
        base = Path(temporary)
        cases = [validation(base/"validation"), lifecycle(base/"lifecycle"), recovery(base/"recovery")]
    sources = ["tasks.py", "task_scheduler.py", "contracts.py", "web/server.py"]
    result = {"format":"ori-tasks-python-taskbook-oracle", "format_version":1,
        "python":sys.version,"sqlite":sqlite3.sqlite_version,
        "generator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256":{name:hashlib.sha256((ROOT/"radioinspector"/name).read_bytes()).hexdigest() for name in sources},
        "reference_book":references(), "cases":cases}
    raw = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(raw, encoding="utf-8")
    else:
        print(raw, end="")


if __name__ == "__main__":
    main()
