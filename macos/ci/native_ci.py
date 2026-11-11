"""Run the unchanged release gate; sample only a stalled owned XCTest process.

Darwin diagnostics are read-only and do not extend the release runner's 600s
command deadline. No process-name-wide signals, HOME changes or test skips.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    started: str
    command: str


def parse_process_table(text: str) -> dict[int, Process]:
    result = {}
    for line in text.splitlines():
        fields = line.split(None, 7)
        if len(fields) != 8 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        value = Process(int(fields[0]), int(fields[1]), " ".join(fields[2:7]), fields[7])
        result[value.pid] = value
    return result


def descendants(root: int, processes: dict[int, Process]) -> list[Process]:
    if root not in processes:
        return []
    known = {root}
    while True:
        children = {p.pid for p in processes.values() if p.parent in known}
        expanded = known | children
        if expanded == known:
            return [processes[pid] for pid in sorted(known - {root})]
        known = expanded


def sampling_candidates(root: int, processes: dict[int, Process], seen: dict[Process, float],
                        sampled: set[Process], *, now: float) -> list[Process]:
    owned = [p for p in descendants(root, processes) if Path(p.command).name == "xctest"]
    live = set(owned)
    for prior in list(seen):
        if prior not in live:
            del seen[prior]
    for process in owned:
        seen.setdefault(process, now)
    return [p for p in owned if p not in sampled and now - seen[p] >= 60]


def process_table() -> dict[int, Process]:
    output = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,lstart=,comm="],
                            capture_output=True, text=True, check=True, timeout=5)
    return parse_process_table(output.stdout)


def monitor(child: subprocess.Popen, directory: Path, stopped: threading.Event) -> None:
    seen, sampled = {}, set()
    directory.mkdir(parents=True, exist_ok=True)
    sampler = shutil.which("sample")
    while not stopped.wait(5):
        try:
            candidates = sampling_candidates(child.pid, process_table(), seen, sampled, now=time.monotonic())
            for process in candidates[:4]:
                if stopped.is_set() or child.poll() is not None:
                    return
                # Recheck both ancestry and start identity immediately before
                # observing a PID; never sample an unrelated/reused process.
                current = process_table()
                if current.get(process.pid) != process or process not in descendants(child.pid, current):
                    continue
                sampled.add(process)
                metadata = {"owned_root": child.pid, "pid": process.pid, "parent": process.parent,
                            "started": process.started, "executable": process.command}
                path = directory / f"xctest-{process.pid}.txt"
                if sampler is None:
                    metadata["diagnostic_error"] = "Darwin sample executable is unavailable"
                else:
                    print(f"[native-diagnostic] Sampling stalled owned XCTest PID {process.pid}", flush=True)
                    try:
                        result = subprocess.run([sampler, str(process.pid), "3", "-file", str(path)],
                                                capture_output=True, text=True, timeout=6)
                        metadata["sample_exit_code"] = result.returncode
                        metadata["sample_message"] = (result.stdout + result.stderr)[-2048:]
                    except subprocess.TimeoutExpired:
                        metadata["diagnostic_error"] = "Owned process sample exceeded its bounded collection window"
                path.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
                if len(sampled) >= 8:
                    return
        except (OSError, subprocess.SubprocessError) as error:
            (directory / "diagnostic-error.json").write_text(json.dumps({"error": type(error).__name__}) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, default=Path("release-reports/native-process-samples"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a release-check command is required")
    if sys.platform != "darwin":
        return subprocess.call(command)
    child = subprocess.Popen(command, start_new_session=True)
    stopped = threading.Event()
    observer = threading.Thread(target=monitor, args=(child, arguments.report_dir, stopped),
                                name="ori-owned-native-diagnostics")
    previous = {}
    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, forward)
    observer.start()
    try:
        return child.wait()
    finally:
        stopped.set()
        observer.join(timeout=15)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
