"""Private API-tool processes using the release runner's existing authority.

The adapter owns child stdin and bounded streams. Only OwnedCommand/WindowsJob
owns termination; a child PID in a diagnostic record is never signal authority.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_retained = {}


class ManagedError(RuntimeError):
    pass


def _publish(path, value):
    raw = json.dumps(value, ensure_ascii=True).encode("ascii")
    if len(raw) > 4096:
        raise ManagedError("private_record_budget")
    descriptor, temporary = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def _record(path, nonce):
    try:
        if path.is_symlink():
            raise ManagedError("unknown_private_record")
        with path.open("rb") as source:
            raw = source.read(4097)
    except FileNotFoundError:
        return None
    if len(raw) > 4096:
        raise ManagedError("private_record_budget")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("nonce") != nonce:
        raise ManagedError("unknown_private_record")
    return value


class _Lifeline:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def close(self):
        if not self.closed:
            _publish(
                self.process.directory / "stop.json", {"nonce": self.process.nonce}
            )
            self.closed = True


class ManagedProcess:
    def __init__(
        self,
        argv,
        *,
        environment=None,
        cwd=None,
        limit=256 * 1024,
        merge=False,
        stdin_pipe=False,
    ):
        self.argv = list(map(str, argv))
        self.environment = environment
        self.cwd = None if cwd is None else str(Path(cwd).resolve())
        self.limit, self.merge = limit, merge
        self.directory = Path(tempfile.mkdtemp(prefix="ori-api-owned-")).resolve()
        self.nonce = uuid.uuid4().hex
        self.owner = self.job = self.bootstrap = None
        self._assigned = False
        self.closed = False
        self._disposed = False
        self._code = self._pid = None
        self.stdin = _Lifeline(self) if stdin_pipe else None
        self.stdout_path = self.directory / "stdout.log"
        self.stderr_path = self.directory / "stderr.log"
        for path in (self.stdout_path, self.stderr_path):
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        spec = {
            "nonce": self.nonce,
            "argv": self.argv,
            "cwd": self.cwd,
            "limit": limit,
            "merge": merge,
            "stdin_pipe": stdin_pipe,
        }
        raw = json.dumps(spec, ensure_ascii=True).encode("ascii")
        if len(raw) > 256 * 1024:
            raise ManagedError("command_record_budget")
        descriptor = os.open(
            self.directory / "command.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
        _publish(
            self.directory / "lease.json",
            {"nonce": self.nonce, "directory": str(self.directory)},
        )
        self._log = open(os.devnull, "wb")  # noqa: SIM115 - retained with the ownership handle
        _retained[self.directory] = self

    @property
    def recovery(self):
        return str(self.directory / "lease.json")

    @property
    def pid(self):
        value = _record(self.directory / "child.json", self.nonce)
        if value is not None:
            if type(value.get("pid")) is not int or value["pid"] <= 0:
                raise ManagedError("unknown_child_identity")
            self._pid = value["pid"]
        return self._pid

    @property
    def returncode(self):
        value = _record(self.directory / "exit.json", self.nonce)
        if value is not None:
            code = value.get("code")
            if type(code) is not int or not -(2**31) <= code < 2**32:
                raise ManagedError("unknown_child_exit")
            self._code = code
        return self._code

    def poll(self):
        return self.returncode

    @property
    def output_exceeded(self):
        return any(
            _record(self.directory / (name + ".overflow.json"), self.nonce) is not None
            for name in ("stdout", "stderr")
        )

    def start(self):
        command = [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--adapter",
            str(self.directory / "command.json"),
        ]
        if os.name == "posix":
            from tests.release_process_ownership import OwnedCommand

            registry = self.directory / "registry"
            registry.mkdir(mode=0o700)
            environment = (
                dict(os.environ) if self.environment is None else self.environment
            )
            inherited = environment.get("ORI_RELEASE_PROCESS_REGISTRY")
            self.owner = OwnedCommand.start(
                command,
                environment=environment,
                registry=Path(inherited) if inherited else registry,
                root=ROOT,
                log=self._log,
                shutdown_timeout=2,
            )
            _publish(
                self.directory / "authority.json",
                {
                    "nonce": self.nonce,
                    "kind": "posix_session",
                    "directory": str(self.owner.directory),
                },
            )
        else:
            from tests.run_release_checks import WindowsJob

            self.job = WindowsJob()
            try:
                self.bootstrap = subprocess.Popen(
                    command + ["--job-gate"],
                    cwd=ROOT,
                    env=self.environment,
                    stdin=subprocess.PIPE,
                    stdout=self._log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                self.job.assign(self.bootstrap)
                self._assigned = True
                self.bootstrap.stdin.write(b"go\n")
                self.bootstrap.stdin.close()
                _publish(
                    self.directory / "authority.json",
                    {
                        "nonce": self.nonce,
                        "kind": "retained_windows_job",
                        "parent_pid": os.getpid(),
                    },
                )
            except BaseException:
                if self.bootstrap is not None and not self.bootstrap.stdin.closed:
                    self.bootstrap.stdin.close()
                self.stop(2)
                raise
        return self

    def wait(self, timeout):
        if self.closed:
            return self.returncode
        deadline = time.monotonic() + timeout
        while True:
            if self.output_exceeded:
                raise ManagedError("output_budget")
            error = _record(self.directory / "error.json", self.nonce)
            if error is not None:
                raise ManagedError("adapter_error: " + error.get("error", "unknown"))
            if self.owner is not None:
                status = self.owner.status()
                if status and (status["phase"] == "failed" or status["cleanup_failed"]):
                    raise ManagedError("owned_session_failed")
                completed = status and status["phase"] == "exited"
                code = status["exit_code"] if completed else None
            else:
                code = self.bootstrap.poll()
                completed = code is not None
            if completed:
                if code != 0:
                    raise ManagedError("adapter_failed")
                record = _record(self.directory / "complete.json", self.nonce)
                if (
                    record is None
                    or record.get("streams_closed") is not True
                    or self.returncode is None
                ):
                    raise ManagedError("incomplete_child_closure")
                if self.owner is not None:
                    if self.owner.authority.members():
                        raise ManagedError("owned_descendant")
                    return self.returncode
                if not self.job.active():
                    return self.returncode
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            time.sleep(0.005)

    def finish(self, timeout=2):
        if self.closed:
            return
        if self.owner is not None:
            self.owner.release(timeout)
        else:
            if self.bootstrap.poll() is None or self.job.active():
                raise ManagedError("unconfirmed_windows_closure")
            self.job.close()
        self.closed = True
        self._log.close()

    def stop(self, timeout=2):
        if self.closed:
            return
        if self.owner is not None:
            self.owner.stop(timeout)
        elif self.job is not None:
            deadline = time.monotonic() + timeout
            if self._assigned:
                self.job.terminate()
            elif self.bootstrap is not None:
                # No go write is possible before assignment. This is the exact
                # Windows process HANDLE for our still-gated bootstrap, not PID
                # authority inferred from an exit/readiness record.
                self.bootstrap.terminate()
            if self.bootstrap is not None:
                self.bootstrap.wait(timeout=max(0.001, deadline - time.monotonic()))
            while self.job.active():
                if time.monotonic() >= deadline:
                    raise ManagedError("unconfirmed_windows_closure")
                time.sleep(0.01)
            self.job.close()
        else:
            raise ManagedError("launch_authority_unavailable")
        self.closed = True
        self._log.close()

    def outputs(self):
        values = []
        for path in (self.stdout_path, self.stderr_path):
            with path.open("rb") as source:
                values.append(source.read(self.limit + 1))
        if any(len(value) > self.limit for value in values):
            raise ManagedError("output_budget")
        return values

    def dispose(self):
        if self._disposed:
            return
        if not self.closed:
            raise ManagedError("cleanup_unconfirmed: " + self.recovery)
        self._code = self.returncode
        self._pid = self.pid
        shutil.rmtree(self.directory)
        self._disposed = True
        _retained.pop(self.directory, None)
        # Release reaped process handles only after the original authority has
        # proved closure. Diagnostic child PID/status remain cached above.
        self.owner = self.job = self.bootstrap = None


def adapter(path, *, gate=False):
    if gate and sys.stdin.buffer.readline() != b"go\n":
        return 125
    with path.open("rb") as source:
        raw = source.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        return 125
    spec = json.loads(raw)
    directory, nonce = path.parent, spec["nonce"]
    child = None
    stopping = threading.Event()
    failures = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_args: stopping.set())

    def record(name, **value):
        _publish(directory / name, {"nonce": nonce, **value})

    def read(pipe, name):
        count, overflow = 0, False
        try:
            with (directory / (name + ".log")).open("wb") as output:
                while block := os.read(pipe.fileno(), 8192):
                    remaining = max(0, spec["limit"] - count)
                    output.write(block[:remaining])
                    count += len(block)
                    if count > spec["limit"] and not overflow:
                        overflow = True
                        record(name + ".overflow.json")
        except Exception as error:  # noqa: BLE001 - every reader failure invalidates IPC proof
            failures.append(type(error).__name__)
        finally:
            pipe.close()

    try:
        child = subprocess.Popen(
            spec["argv"],
            cwd=spec["cwd"],
            stdin=subprocess.PIPE if spec["stdin_pipe"] else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if spec["merge"] else subprocess.PIPE,
        )
        record("child.json", pid=child.pid)
        readers = [threading.Thread(target=read, args=(child.stdout, "stdout"))]
        if not spec["merge"]:
            readers.append(threading.Thread(target=read, args=(child.stderr, "stderr")))
        for reader in readers:
            reader.start()
        published = False
        while True:
            if failures:
                raise ManagedError("bounded_output_io_failed")
            stop = _record(directory / "stop.json", nonce)
            if (
                child.stdin is not None
                and not child.stdin.closed
                and (stopping.is_set() or stop is not None)
            ):
                child.stdin.close()
            code = child.poll()
            if code is not None:
                if not published:
                    record("exit.json", code=code)
                    published = True
                if not any(reader.is_alive() for reader in readers):
                    if failures:
                        raise ManagedError("bounded_output_io_failed")
                    if child.stdin is not None and not child.stdin.closed:
                        child.stdin.close()
                    record("complete.json", streams_closed=True)
                    return 0
            time.sleep(0.005)
    except Exception as error:  # noqa: BLE001 - publish failure; original owner retains cleanup
        record("error.json", error=(type(error).__name__ + ": " + str(error))[:512])
        if child is not None and child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        return 125  # Original owner retains all children, pipes and cleanup authority.


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4) or sys.argv[1] != "--adapter":
        raise SystemExit(125)
    raise SystemExit(adapter(Path(sys.argv[2]), gate=sys.argv[-1] == "--job-gate"))
