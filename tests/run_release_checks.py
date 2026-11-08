"""Run the complete synthetic release checks, without accessing physical devices.

Usage: python -m tests.run_release_checks [--swift]
Requires NumPy, pySerial, Node and a working Tk display (use xvfb-run on Linux).
--swift additionally runs both macOS packages and an unsigned Xcode build.
Only private child processes, ephemeral loopback servers and temporary archives
are owned here. Hardware acceptance remains a separate, explicit operation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import faulthandler
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit

from tests.loopback_dns import local_loopback_names


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = (
    ("ORI_AVERAGE_SMOKE_BASE_URL", "tests.average_smoke_server"),
    ("ORI_AVERAGE_DETECTION_SMOKE_BASE_URL", "tests.sweep_change_detection_smoke_server"),
    ("ORI_ROUNDING_SMOKE_BASE_URL", "tests.frequency_rounding_smoke_server"),
    ("ORI_CONTEXT_SMOKE_BASE_URL", "tests.sweep_context_smoke_server"),
    ("ORI_THREE_THRESHOLDS_SMOKE_BASE_URL", "tests.three_thresholds_smoke_server"),
    ("ORI_MASK_SMOKE_BASE_URL", "tests.spectral_mask_smoke_server"),
    ("ORI_THRESHOLD_SMOKE_BASE_URL", "tests.adaptive_threshold_smoke_server"),
)
# Match both the existing test identity and its reason. A generic "POSIX" reason
# would accidentally allow new HTTP/Tk tests to disappear on Windows.
WINDOWS_SKIPPED_CLASSES = {
    "tests.test_serial_transport.PosixSerialTransportTests": "POSIX pseudo terminals required",
    "tests.test_tinysa_setup_guard_pty.TinySASetupPTYTests": "POSIX pseudo terminals required",
    "tests.test_windows_serial_transport.PendingInputPosixParityTests": "Own PTY required",
}
WINDOWS_SKIPPED_TESTS = {
    "tests.test_task_scheduler.TaskSchedulerTests.test_atomic_save_does_not_follow_predictable_tmp_symlink": "POSIX permission bits",
    "tests.test_task_scheduler.TaskSchedulerTests.test_atomic_save_preserves_permissions_and_defaults_to_owner_only": "POSIX permission bits",
}


class ReleaseCheckError(RuntimeError):
    """A release gate failed, including incomplete cleanup or verification."""


def child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # Do not allow inherited fixture URLs to direct a test at another archive.
    for key in tuple(environment):
        if key.startswith("ORI_") and key.endswith("_SMOKE_BASE_URL"):
            environment.pop(key)
    environment.pop("THREE_THRESHOLDS_SMOKE_URL", None)
    for key in ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONINSPECT"):
        environment.pop(key, None)
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                       PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", ORI_WINDOWS_RELEASE_TK="1",
                       ORI_ROUNDING_TK_SMOKE="1")
    # The test environment is local to our children, never the caller's shell.
    for key in ("NO_PROXY", "no_proxy"):
        values = environment.get(key, "").split(",")
        environment[key] = ",".join(dict.fromkeys(v for v in [*values, "127.0.0.1", "localhost", "::1"] if v))
    environment["PYTHONPATH"] = str(ROOT)
    return environment


def check_dependencies() -> None:
    if shutil.which("node") is None:
        raise ReleaseCheckError("Node is required; install it and add it to PATH")
    for module in ("numpy", "serial", "tkinter"):
        try:
            importlib.import_module(module)
        except ImportError as error:
            raise ReleaseCheckError(f"Required dependency {module!r} is unavailable: {error}") from error
    import tkinter
    print(f"[release-runtime] Python={sys.version.split()[0]} platform={sys.platform}", flush=True)
    try:
        root = tkinter.Tk()
        try:
            root.withdraw()
            root.update_idletasks()
            print(f"[release-runtime] Tcl={root.tk.call('info', 'patchlevel')} "
                  f"Tk={root.tk.call('package', 'provide', 'Tk')} "
                  f"windowing={root.tk.call('tk', 'windowingsystem')}", flush=True)
        finally:
            root.destroy()
    except tkinter.TclError as error:
        raise ReleaseCheckError("Tk needs a working display; on headless Linux use xvfb-run -a. " + str(error)) from error
    print("Required dependencies available: NumPy, pySerial, Node, Tk", flush=True)


def unexpected_skips(skipped: list[tuple[str, str]], *, platform: str = os.name) -> list[tuple[str, str]]:
    def allowed(test: str, reason: str) -> bool:
        if platform != "nt":
            return False
        expected = WINDOWS_SKIPPED_TESTS.get(test)
        if expected is None:
            expected = WINDOWS_SKIPPED_CLASSES.get(test.rsplit(".", 1)[0])
        return expected == reason
    return [(test, reason) for test, reason in skipped if not allowed(test, reason)]


class DiagnosticTestResult(unittest.TextTestResult):
    """Timestamp test boundaries without changing their execution or outcome."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._run_started = time.monotonic()
        self._test_started = self._run_started

    def _event(self, action: str, test, now: float) -> None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        duration = f" duration={now - self._test_started:.3f}s" if action == "stop" else ""
        self.stream.writeln(f"\n[release-test-{action}] utc={timestamp} elapsed={now - self._run_started:.3f}s{duration} id={test.id()}")
        self.stream.flush()

    def startTest(self, test):
        self._test_started = time.monotonic()
        self._event("start", test, self._test_started)
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        self._event("stop", test, time.monotonic())


# Narrow test-infrastructure adaptation of PR #96 at 1b8b810a6141322d1e89a1d6c881f4ce7477961c.
# Keep lifecycle/worker-GC regressions in-process: they deliberately reuse Tk.
MACOS_TK_CLASSES = frozenset({
    "tests.test_windows_release_flows.WindowsReleaseFlowTests",
    "tests.test_windows_sweep_context.ActualTkHTTPTests",
    "tests.test_windows_three_thresholds.ActualTkHTTPTests",
    "tests.test_windows_frequency_rounding.ActualTkHTTPTests",
    "tests.test_windows_sweep_average.AverageActualTkHTTPTests",
})
TK_CASE_TIMEOUT = 90


class IsolatedTkTest(unittest.TestCase):
    """One original case per fresh Python; never turn a broken child into a skip."""
    def __init__(self, test_id):
        super().__init__("runTest")
        self.test_id = test_id

    def id(self):
        return self.test_id

    def __str__(self):
        return self.test_id + " (isolated Tk process)"

    def runTest(self):
        with tempfile.TemporaryDirectory(prefix="ori-tk-case-") as temporary:
            report_path = Path(temporary) / "result.json"
            # No new session/job: descendants remain owned by the outer runner.
            # Inherit its private HOME/temp, registry and already-created URLs.
            # Write directly to the outer log, not a PIPE or deferred buffer:
            # even an outer timeout retains the child's diagnostics immediately.
            completed = subprocess.run(
                [sys.executable, "-m", "tests.run_release_checks",
                 "--_single-test", self.test_id, "--_diagnostic-after", "30",
                 "--_python-suite", str(report_path)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=sys.stderr,
                stderr=subprocess.STDOUT, timeout=TK_CASE_TIMEOUT)
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ReleaseCheckError(f"{self.test_id}: missing or malformed child report") from error
            if (not isinstance(report, dict) or
                    type(report.get("tests_run")) is not int or report["tests_run"] != 1 or
                    report.get("selected_test") != self.test_id or
                    report.get("successful") is not True):
                raise ReleaseCheckError(f"{self.test_id}: child did not successfully run exactly the requested case")
            skipped = report.get("skipped")
            if (not isinstance(skipped, list) or len(skipped) > 1 or
                    any(not isinstance(entry, list) or len(entry) != 2 or
                        entry[0] != self.test_id or not isinstance(entry[1], str)
                        for entry in skipped)):
                raise ReleaseCheckError(f"{self.test_id}: invalid child skips")
            unexpected = [list(entry) for entry in unexpected_skips(skipped)]
            if (report.get("unexpected_skips") != unexpected or unexpected or
                    completed.returncode != 0):
                raise ReleaseCheckError(f"{self.test_id}: child exit/report mismatch (status {completed.returncode})")
            if skipped:
                self.skipTest(skipped[0][1])  # Only the unchanged allowlist can reach here.


def isolate_tk_cases(suite):
    result = unittest.TestSuite()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            result.addTest(isolate_tk_cases(item))
        else:
            result.addTest(IsolatedTkTest(item.id())
                           if item.id().rsplit(".", 1)[0] in MACOS_TK_CLASSES else item)
    return result


def _case_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _case_ids(item)
        else:
            yield item.id()


def run_python_suite(report_path: Path, *, diagnostics: bool = False, only_test: str | None = None) -> int:
    with local_loopback_names():
        if only_test is not None:
            suite = unittest.defaultTestLoader.loadTestsFromName(only_test)
            if list(_case_ids(suite)) != [only_test]:
                raise ReleaseCheckError("Single-test selection must resolve to exactly the requested case")
        else:
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
            if sys.platform == "darwin" or os.environ.get("ORI_RELEASE_ISOLATE_TK") == "1":
                suite = isolate_tk_cases(suite)
        result = unittest.TextTestRunner(verbosity=2, resultclass=DiagnosticTestResult if diagnostics else None).run(suite)
    skipped = [(test.id(), reason) for test, reason in result.skipped]
    unexpected = unexpected_skips(skipped)
    report = {"tests_run": result.testsRun, "successful": result.wasSuccessful(),
              "skipped": skipped, "unexpected_skips": unexpected}
    if only_test is not None:
        report["selected_test"] = only_test
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for test, reason in unexpected:
        print(f"UNEXPECTED SKIP: {test}: {reason}", file=sys.stderr)
    return 0 if result.wasSuccessful() and result.testsRun > 0 and not unexpected else 1


def _run_diagnostic_python_suite(report_path: Path, diagnostic_after: float, *, only_test: str | None = None) -> int:
    # faulthandler has a process-global C watchdog. Only the dedicated CLI child
    # owns it; nested run_python_suite unit regressions never arm or cancel it.
    # Periodic dumps work even when Tk blocks while holding the Python GIL.
    faulthandler.dump_traceback_later(diagnostic_after, repeat=True, file=sys.__stderr__, exit=False)
    try:
        return run_python_suite(report_path, diagnostics=True, **({"only_test": only_test} if only_test is not None else {}))
    finally:
        faulthandler.cancel_dump_traceback_later()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _process_options() -> dict:
    # A console Ctrl-C belongs to the orchestrator. Servers shut down by private
    # stdin EOF on every OS; do not broadcast signals or kill named processes.
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


class WindowsJob:
    """Private kill-on-close Job Object; no process-name or device enumeration.

    The supervisor waits on a private pipe until assignment succeeds, so even a
    fast test command cannot create descendants outside this job.
    """
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in
                        ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
            _fields_ += [(name, wintypes.DWORD) for name in
                         ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]

        self.ctypes = ctypes
        self.Accounting = Accounting
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, argtypes, restype in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(self.api, name)
            function.argtypes, function.restype = argtypes, restype
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def active(self) -> int:
        accounting = self.Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(accounting), self.ctypes.sizeof(accounting), None):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return accounting.ActiveProcesses

    def terminate(self) -> None:
        if not self.api.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise self.ctypes.WinError(self.ctypes.get_last_error())


def _signal_group(process: subprocess.Popen, signum: int) -> bool:
    try:
        os.killpg(process.pid, signum)
        return True
    except ProcessLookupError:
        return False


def _register_group(process: subprocess.Popen, environment: dict[str, str]) -> Path | None:
    registry = environment.get("ORI_RELEASE_PROCESS_REGISTRY")
    if os.name != "posix" or not registry:
        return None
    record = Path(registry) / f"{process.pid}.pid"
    record.write_text(str(process.pid), encoding="ascii")
    return record


def _unregister_group(record: Path | None) -> None:
    if record is not None:
        record.unlink(missing_ok=True)


def _retire_stopped_group(record: Path | None, process: subprocess.Popen) -> None:
    if record is None:
        return
    process.poll()
    try:
        stopped = not _signal_group(process, 0)
    except OSError:
        # Keep the ownership record for the outer scope if cleanup is uncertain.
        return
    if stopped:
        _unregister_group(record)


def _registered_groups(registry: Path):
    for record in registry.rglob("*.pid"):
        if (not re.fullmatch(r"[1-9][0-9]*", record.stem) or int(record.stem) <= 1 or
                record.is_symlink() or record.read_text(encoding="ascii") != record.stem):
            raise ReleaseCheckError("Invalid owned process-group registration")
        yield record, SimpleNamespace(pid=int(record.stem), poll=lambda: None, wait=lambda **_kwargs: None)


def _cleanup_registry(registry: Path, timeout: float) -> None:
    errors = []
    for record, group in _registered_groups(registry):
        try:
            _stop_command(group, timeout)
            record.unlink(missing_ok=True)
        except (OSError, ReleaseCheckError, subprocess.SubprocessError) as error:
            errors.append(f"owned group {group.pid}: {error}")
    if errors:
        raise ReleaseCheckError("Could not finish nested command cleanup: " + "; ".join(errors))


def _stop_command(process: subprocess.Popen, timeout: float, job: WindowsJob | None = None) -> None:
    if job is not None:
        job.terminate()
        process.wait(timeout=timeout)
        deadline = time.monotonic() + timeout
        while job.active():
            if time.monotonic() >= deadline:
                raise ReleaseCheckError("Timed out reaping the owned Windows job")
            time.sleep(0.05)
    else:
        # Popen created this session/group, including any Node/XCTest children.
        # The group can still exist after its immediate parent has exited.
        process.poll()
        _signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGKILL)
            process.wait(timeout=timeout)
        deadline = time.monotonic() + timeout
        while True:
            process.poll()  # Reap our direct child while descendants stop.
            try:
                if not _signal_group(process, 0):
                    break
            except PermissionError:
                # Darwin can transiently report EPERM while a signalled group
                # is exiting. Retry the read-only liveness check, never assume
                # that an inaccessible group has stopped.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
                continue
            if time.monotonic() >= deadline:
                _signal_group(process, signal.SIGKILL)
                break
            time.sleep(0.05)
        process.wait(timeout=timeout)
        deadline = time.monotonic() + timeout
        while _signal_group(process, 0):
            if time.monotonic() >= deadline:
                raise ReleaseCheckError(f"Owned process group {process.pid} did not stop")
            time.sleep(0.05)


def run_command(command: list[str], *, name: str, environment: dict[str, str],
                directory: Path, timeout: float, shutdown_timeout: float = 15) -> str:
    """Capture UTF-8 diagnostics and reap only this command's owned process tree."""
    path = directory / (name + ".log")
    print(f"\n== {name}: {' '.join(command)} ==", flush=True)
    environment = environment.copy()
    inherited_registry = environment.get("ORI_RELEASE_PROCESS_REGISTRY")
    owns_registry = os.name == "posix"
    registry = directory / (name + ".groups")
    if owns_registry:
        # Every command owns a subtree, even inside another supervised command.
        # Cleanup sees its descendants but cannot touch a sibling's groups.
        parent_registry = Path(inherited_registry) if inherited_registry else directory
        registry = Path(tempfile.mkdtemp(prefix=name + ".groups-", dir=parent_registry))
        environment["ORI_RELEASE_PROCESS_REGISTRY"] = str(registry)
    with path.open("w", encoding="utf-8") as log:
        job = WindowsJob() if sys.platform == "win32" else None
        process = None
        record = None
        try:
            actual = [sys.executable, "-m", "tests.run_release_checks", "--_supervise-command", json.dumps(command)]
            process = subprocess.Popen(actual, cwd=ROOT, env=environment,
                                       stdin=subprocess.PIPE,
                                       stdout=log, stderr=subprocess.STDOUT, **_process_options())
            if job is None:
                record = _register_group(process, environment)
            if job is not None:
                job.assign(process)
            process.stdin.write(b"go\n")
            process.stdin.close()
            process.wait(timeout=timeout)
            if job is not None and job.active():
                raise ReleaseCheckError(f"{name}: command left an active owned descendant")
            if job is None and _signal_group(process, 0):
                raise ReleaseCheckError(f"{name}: command left an active owned descendant")
            if process is not None:
                _retire_stopped_group(record, process)
            record = None
            if owns_registry:
                for nested_record, group in _registered_groups(registry):
                    if _signal_group(group, 0):
                        raise ReleaseCheckError(f"{name}: command left an active nested descendant")
                    nested_record.unlink(missing_ok=True)
        except BaseException as error:
            cleanup_errors = []
            if process is not None:
                # Before a successful job assignment the supervisor can only
                # wait on its pipe; close it, then terminate that direct child.
                try:
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()
                        if process.poll() is None:
                            process.terminate()
                        process.wait(timeout=shutdown_timeout)
                    else:
                        _stop_command(process, shutdown_timeout, job)
                except BaseException as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            if process is not None:
                _retire_stopped_group(record, process)
            record = None
            if owns_registry:
                try:
                    _cleanup_registry(registry, shutdown_timeout)
                except BaseException as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            print(_read(path), end="", flush=True)
            if cleanup_errors:
                raise ReleaseCheckError(f"{name}: {error}; cleanup failed: " + "; ".join(cleanup_errors)) from error
            raise
        finally:
            if job is not None:
                job.close()
            if process is not None:
                _retire_stopped_group(record, process)
    output = _read(path)
    print(output, end="" if output.endswith("\n") else "\n", flush=True)
    if process.returncode:
        raise ReleaseCheckError(f"{name} exited with status {process.returncode}")
    return output


class Fixture:
    def __init__(self, module: str, directory: Path, environment: dict[str, str], *,
                 command: list[str] | None = None):
        self.module = module
        self.path = directory / (module + ".stdout.log")
        self.stderr_path = directory / (module + ".stderr.log")
        self.before: str | None = None
        self.record: Path | None = None
        self.job: WindowsJob | None = None
        self.process: subprocess.Popen | None = None
        self.log = self.path.open("w", encoding="utf-8")
        self.stderr = self.stderr_path.open("w", encoding="utf-8")
        try:
            self.job = WindowsJob() if sys.platform == "win32" else None
            actual = command if command is not None else [sys.executable, "-m", module, "--port", "0", "--shutdown-stdin"]
            self.process = subprocess.Popen(
                [sys.executable, "-m", "tests.run_release_checks", "--_supervise-command", json.dumps(actual)],
                cwd=ROOT, env=environment, stdin=subprocess.PIPE,
                stdout=self.log, stderr=self.stderr, **_process_options())
            self.record = _register_group(self.process, environment)
            if self.job is not None:
                self.job.assign(self.process)
            self.process.stdin.write(b"go\n")
            self.process.stdin.flush()
        except BaseException:
            if self.process is not None:
                if self.process.stdin is not None:
                    self.process.stdin.close()
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=15)
            if self.job is not None:
                self.job.close()
            self.log.close()
            self.stderr.close()
            raise

    def diagnostics(self) -> str:
        return _read(self.path) + _read(self.stderr_path)

    def ready(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            output = _read(self.path)
            # Do not parse a partially written first line.
            if "\n" in output:
                try:
                    metadata = json.loads(output.splitlines()[0])
                    if not isinstance(metadata, dict) or not isinstance(metadata.get("url"), str):
                        raise ValueError("expected a JSON object with a URL string")
                    url = urlsplit(metadata["url"])
                    before = metadata["dump_sha256_before"]
                    if (url.scheme != "http" or url.hostname != "127.0.0.1" or
                            not url.port or url.username or url.password or url.query or url.fragment or
                            url.path not in ("", "/", "/platform") or
                            not isinstance(before, str) or not re.fullmatch(r"[a-f0-9]{64}", before)):
                        raise ValueError("expected an ephemeral loopback URL and SHA-256 digest")
                    self.before = before
                except (ValueError, KeyError, TypeError) as error:
                    raise ReleaseCheckError(f"{self.module}: invalid startup metadata: {error}\n{self.diagnostics()}") from error
                if self.process.poll() is not None:
                    raise ReleaseCheckError(f"{self.module}: exited during startup\n{self.diagnostics()}")
                return f"http://127.0.0.1:{url.port}"
            if self.process.poll() is not None or time.monotonic() >= deadline:
                raise ReleaseCheckError(f"{self.module}: startup failed or timed out\n{self.diagnostics()}")
            time.sleep(0.05)

    def close(self, timeout: float) -> None:
        forced = False
        try:
            # Closing only our pipe is the portable graceful shutdown protocol.
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                forced = True
                _stop_command(self.process, timeout, self.job)
            if ((self.job is not None and self.job.active()) or
                    (self.job is None and _signal_group(self.process, 0))):
                forced = True
                _stop_command(self.process, timeout, self.job)
            output = _read(self.path)
            try:
                final = json.loads(output.splitlines()[-1])
            except (ValueError, IndexError):
                final = {}
            if (forced or self.process.returncode != 0 or self.before is None or
                    not isinstance(final, dict) or final.get("unchanged") is not True or
                    final.get("dump_sha256_after") != self.before):
                reason = "forced cleanup; archive integrity unverified" if forced else "cleanup/archive verification failed"
                raise ReleaseCheckError(f"{self.module}: {reason}\n{self.diagnostics()}")
            print(f"{self.module}: server stopped, archive unchanged ({self.before})", flush=True)
        finally:
            if self.job is not None:
                self.job.close()
            _retire_stopped_group(self.record, self.process)
            self.log.close()
            self.stderr.close()


def validate_swift_output(output: str, name: str) -> None:
    executed = re.findall(r"Executed (\d+) tests?", output)
    skipped = [line for line in output.splitlines()
               if re.search(r"\bskipped\b", line, re.IGNORECASE)
               and not re.search(r"\b(?:with )?0 tests? skipped\b", line)]
    if skipped:
        raise ReleaseCheckError(f"{name}: unexpected Swift skips:\n" + "\n".join(skipped))
    if not any(int(count) > 0 for count in executed):
        raise ReleaseCheckError(f"{name}: no executed XCTest tests were reported")


def release_checks(*, swift: bool = False, startup_timeout: float = 20,
                   command_timeout: float = 600, shutdown_timeout: float = 15) -> None:
    if swift and sys.platform != "darwin":
        raise ReleaseCheckError("--swift requires macOS with Xcode")
    if swift and (shutil.which("swift") is None or shutil.which("xcodebuild") is None):
        raise ReleaseCheckError("--swift requires swift and xcodebuild on PATH")
    environment = child_environment()
    environment.pop("ORI_RELEASE_PROCESS_REGISTRY", None)
    children: list[Fixture] = []
    failure: BaseException | None = None
    cleanup_errors = []
    with tempfile.TemporaryDirectory(prefix="ori-release-checks-") as temporary:
        directory = Path(temporary)
        # Some server tests use tempfile's default for their synthetic task book.
        # Give every fresh child a private temp root before it imports tempfile.
        child_temp = directory / "children"
        child_temp.mkdir()
        environment.update({key: str(child_temp) for key in ("TMPDIR", "TEMP", "TMP")})
        # Tk() automatically executes ~/.Tk.py and sources ~/.Tk.tcl. An empty
        # child-only HOME prevents user startup profiles from escaping synthetic
        # test scope. It also keeps build caches out of the user's home directory.
        child_home = directory / "home"
        child_home.mkdir()
        environment["HOME"] = str(child_home)
        try:
            run_command([sys.executable, "-m", "tests.run_release_checks", "--_dependency-check"],
                        name="dependencies", environment=environment, directory=directory,
                        timeout=startup_timeout, shutdown_timeout=shutdown_timeout)
            run_command([shutil.which("node"), "--version"], name="node", environment=environment,
                        directory=directory, timeout=startup_timeout, shutdown_timeout=shutdown_timeout)
            for key, module in FIXTURES:
                fixture = Fixture(module, directory, environment)
                children.append(fixture)  # Register ownership before readiness can fail.
                environment[key] = fixture.ready(startup_timeout)
                print(f"{module}: {environment[key]}", flush=True)
            environment["THREE_THRESHOLDS_SMOKE_URL"] = environment["ORI_THREE_THRESHOLDS_SMOKE_BASE_URL"]
            report_path = directory / "python-results.json"
            run_command([sys.executable, "-m", "tests.run_release_checks", "--_diagnostic-after",
                         str(min(60.0, command_timeout * 0.8)), "--_python-suite", str(report_path)],
                        name="python-node-tk", environment=environment, directory=directory,
                        timeout=command_timeout, shutdown_timeout=shutdown_timeout)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (not report.get("successful") or report.get("tests_run", 0) <= 0 or
                    report.get("unexpected_skips") or unexpected_skips(report.get("skipped", []))):
                raise ReleaseCheckError("Python suite report did not pass the release gate")
            print(f"Python/Node/Tk: {report['tests_run']} tests, {len(report['skipped'])} allowed platform skips", flush=True)
            if swift:
                for name, package in (("swift-sdk", "macos/SpektrKomandy"), ("swift-native", "macos")):
                    output = run_command(["swift", "test", "--package-path", package,
                                          "--scratch-path", str(directory / (name + "-build"))],
                                         name=name, environment=environment, directory=directory,
                                         timeout=command_timeout, shutdown_timeout=shutdown_timeout)
                    validate_swift_output(output, name)
                run_command(["xcodebuild", "-project", "macos/DvoynoyObzor.xcodeproj", "-scheme", "DvoynoyObzor",
                             "-configuration", "Debug", "-destination", "platform=macOS",
                             "-derivedDataPath", str(directory / "DerivedData"),
                             "CODE_SIGNING_ALLOWED=NO", "build"], name="xcode-unsigned", environment=environment,
                            directory=directory, timeout=command_timeout, shutdown_timeout=shutdown_timeout)
            else:
                print("macOS Swift/native/Xcode checks not requested (use --swift on macOS).", flush=True)
        except BaseException as error:
            failure = error
        finally:
            for fixture in reversed(children):
                try:
                    fixture.close(shutdown_timeout)
                except BaseException as error:
                    cleanup_errors.append(str(error))
        if cleanup_errors:
            prefix = f"{type(failure).__name__}: {failure}\n" if failure else ""
            raise ReleaseCheckError(prefix + "Fixture cleanup failed:\n" + "\n".join(cleanup_errors)) from failure
        if failure is not None:
            raise failure
    print("Synthetic release checks passed. Physical hardware was not validated.", flush=True)


def _interrupted(signum: int, _frame) -> None:
    # A repeated console interrupt must not abandon bounded child cleanup.
    for interrupt in (signal.SIGINT, signal.SIGTERM):
        signal.signal(interrupt, signal.SIG_IGN)
    raise KeyboardInterrupt(f"received signal {signum}")


def positive_seconds(value: str) -> float:
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swift", action="store_true", help="Run both macOS Swift suites and an unsigned Xcode build")
    parser.add_argument("--startup-timeout", type=positive_seconds, default=20, metavar="SECONDS")
    parser.add_argument("--command-timeout", type=positive_seconds, default=600, metavar="SECONDS")
    parser.add_argument("--shutdown-timeout", type=positive_seconds, default=15, metavar="SECONDS")
    parser.add_argument("--_python-suite", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_single-test", help=argparse.SUPPRESS)
    parser.add_argument("--_dependency-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_diagnostic-after", type=positive_seconds, default=60, help=argparse.SUPPRESS)
    parser.add_argument("--_supervise-command", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._single_test is not None and args._python_suite is None:
        parser.error("--_single-test requires --_python-suite")
    if args._supervise_command is not None:
        if sys.stdin.buffer.readline() != b"go\n":
            return 1
        command = json.loads(args._supervise_command)
        if os.name == "posix":
            os.execvpe(command[0], command, os.environ)
        return subprocess.call(command)
    if args._python_suite is not None:
        return _run_diagnostic_python_suite(args._python_suite, args._diagnostic_after,
                                            **({"only_test": args._single_test} if args._single_test is not None else {}))
    if args._dependency_check:
        check_dependencies()
        return 0
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, _interrupted)
        release_checks(swift=args.swift, startup_timeout=args.startup_timeout,
                       command_timeout=args.command_timeout, shutdown_timeout=args.shutdown_timeout)
    except KeyboardInterrupt as error:
        print(f"Release checks interrupted: {error}", file=sys.stderr)
        return 130
    except (ReleaseCheckError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"Release checks FAILED: {error}", file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
