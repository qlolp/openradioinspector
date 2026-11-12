"""Run the complete synthetic release checks, without accessing physical devices.

Usage: python -m tests.run_release_checks [--swift]
       python -m tests.run_release_checks --modern --research-python PATH [--swift]
Requires NumPy, pySerial, Node and a working Tk display (use xvfb-run on Linux).
--swift additionally runs both macOS packages and an unsigned Xcode build.
--modern selects the additive modernization profile; its --swift checks only
the portable contracts package. Full releases require both profiles.
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
import threading
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit

from tests.loopback_dns import local_loopback_names
from tests import release_process_ownership as process_ownership


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
    "tests.test_run_tests_launcher.TestShellLauncher.test_active_environment_takes_precedence_over_local_venv": "POSIX shell launcher",
    "tests.test_run_tests_launcher.TestShellLauncher.test_uv_installs_local_project_with_serial_and_test_extras": "POSIX shell launcher",
    "tests.test_release_v040.SerialReadCancellationTests.test_cancel_interrupts_owned_read_without_reusing_port": "POSIX serial ownership",
    "tests.test_release_v040.TaskBookPermissionTests.test_saving_never_chmods_a_replaced_temporary_path": "POSIX permissions",
    "tests.test_release_v040.TaskBookPermissionTests.test_world_writable_parent_is_rejected_before_saving": "POSIX permissions",
    "tests.test_release_v040.PosixPortOwnershipTests.test_second_open_is_rejected_before_changing_terminal_settings": "POSIX serial ownership",
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


# Keep Tcl-only lifecycle/worker-GC regressions in-process: they deliberately
# verify creator-thread disposal. Real GUI graphs belong to a fresh process;
# delayed native interpreter GC on an acquisition thread aborts on all OSes.
NATIVE_TK_CLASSES = frozenset({
    "tests.test_windows_release_flows.WindowsReleaseFlowTests",
    "tests.test_windows_sweep_context.ActualTkHTTPTests",
    "tests.test_windows_three_thresholds.ActualTkHTTPTests",
    "tests.test_windows_frequency_rounding.ActualTkHTTPTests",
    "tests.test_windows_sweep_average.AverageActualTkHTTPTests",
    "tests.test_windows_monitoring.MonitoringTkTests",
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
                           if item.id().rsplit(".", 1)[0] in NATIVE_TK_CLASSES else item)
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
            # ELF relocation/AF_UNIX packaging belongs to the Linux target.
            # Load that suite explicitly instead of creating platform skips in
            # every Windows/macOS release. Never mask empty base discovery.
            if suite.countTestCases() and sys.platform.startswith("linux"):
                suite.addTests(unittest.defaultTestLoader.loadTestsFromName(
                    "tests.offline_core_release_checks"))
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


class _GilSafeWatchdog:
    """Dump from an explicitly joined Python thread while it holds the GIL."""
    def __init__(self, interval, stream):
        self.interval, self.stream = interval, stream
        self.stopped = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name="ori-diagnostic-watchdog", daemon=True)

    def _run(self):
        while not self.stopped.wait(self.interval):
            try:
                self.stream.write(f"Timeout ({self.interval:.6f}s): GIL-safe diagnostic watchdog\n")
                self.stream.flush()
                # Called by a Python thread holding the GIL, rather than the
                # unsynchronized native timer walking changing Python frames.
                faulthandler.dump_traceback(file=self.stream, all_threads=True)
            except BaseException as error:
                self.error = error
                return

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise ReleaseCheckError("Diagnostic watchdog did not stop")
        if self.error is not None:
            raise ReleaseCheckError(f"Diagnostic watchdog failed: {self.error}") from self.error


def _run_diagnostic_python_suite(report_path: Path, diagnostic_after: float, *, only_test: str | None = None) -> int:
    # CPython gh-140815 was fixed in 3.13.10 / 3.14.1, without a 3.12
    # backport. Darwin 3.14/3.15 also had partial native timer dumps followed by
    # a post-suite hang; native cancellation joins that dump without a bound.
    # Use the existing joined Python watchdog for this observed profile too.
    # A Python watchdog cannot dump while native code holds the GIL forever;
    # the owned outer timeout still terminates that failed command.
    # Product tests and their deadlines are unchanged.
    # Fatal handlers belong only to this dedicated diagnostic child.
    faulthandler.enable(file=sys.__stderr__, all_threads=True)
    version = tuple(sys.version_info[:3])
    fixed = version >= (3, 14, 1) or (3, 13, 10) <= version < (3, 14, 0)
    darwin_observed = sys.platform == "darwin" and version[:2] in ((3, 14), (3, 15))
    if not fixed or darwin_observed:
        watchdog = _GilSafeWatchdog(diagnostic_after, sys.__stderr__)
        watchdog.start()
        try:
            return run_python_suite(report_path, diagnostics=True, **({"only_test": only_test} if only_test is not None else {}))
        finally:
            watchdog.close()
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


def _linux_group_has_live_members(group: int, proc_root: Path = Path("/proc")) -> bool | None:
    """A Linux zombie has no I/O; PID 1 in containers may not reap it.

    Inspect only membership/state, never signal a process found by scanning.
    Unknown or unreadable evidence stays unknown (cleanup must fail closed).
    """
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                text = (entry / "stat").read_text()
            except FileNotFoundError:
                continue
            fields = text[text.rfind(")") + 2:].split()
            if len(fields) < 3:
                return None
            if int(fields[2]) == group and fields[0] not in ("Z", "X"):
                return True
        return False
    except (OSError, ValueError):
        return None


def _signal_group(process: subprocess.Popen, signum: int) -> bool:
    try:
        os.killpg(process.pid, signum)
        if signum == 0 and sys.platform.startswith("linux"):
            running = _linux_group_has_live_members(process.pid)
            if running is not None:
                return running
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


def _cleanup_registry(registry: Path, timeout: float, *, exclude_session: Path | None = None) -> None:
    errors = []
    deadline = time.monotonic() + timeout
    for owner in process_ownership.registered_commands(registry, exclude=exclude_session):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:raise process_ownership.OwnershipError("Nested cleanup deadline; ownership retained")
            owner.stop(remaining)
        except (OSError, process_ownership.OwnershipError, TimeoutError) as error:
            errors.append(str(error))
    for record, group in _registered_groups(registry):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:raise ReleaseCheckError("Nested cleanup deadline; ownership retained")
            _stop_command(group, remaining)
            record.unlink(missing_ok=True)
        except (OSError, ReleaseCheckError, subprocess.SubprocessError) as error:
            errors.append(f"owned group {group.pid}: {error}")
    if errors:
        raise ReleaseCheckError("Could not finish nested command cleanup: " + "; ".join(errors))


def _job_still_active(job: WindowsJob, timeout: float) -> bool:
    """Wait for Windows job accounting after its parent handle was signalled.

    Query only: a genuinely live descendant still times out and triggers the
    existing forced cleanup/fail-closed gate. Never infer archive integrity
    from the parent's zero exit alone.
    """
    deadline=time.monotonic()+timeout
    while job.active():
        remaining=deadline-time.monotonic()
        if remaining<=0:return True
        time.sleep(min(.01,remaining))
    return False


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


def _run_posix_command(command: list[str], *, name: str, environment: dict[str, str],
                       directory: Path, timeout: float, shutdown_timeout: float) -> str:
    environment = environment.copy()
    parent_registry = Path(environment.get("ORI_RELEASE_PROCESS_REGISTRY", directory))
    registry = Path(tempfile.mkdtemp(prefix=name + ".groups-", dir=parent_registry))
    environment["ORI_RELEASE_PROCESS_REGISTRY"] = str(registry)
    path = directory / (name + ".log")
    print(f"\n== {name}: {' '.join(command)} ==", flush=True)
    owner = None
    with path.open("w", encoding="utf-8") as log:
        try:
            owner = process_ownership.OwnedCommand.start(command, environment=environment,
                registry=registry, root=ROOT, log=log, shutdown_timeout=shutdown_timeout)
            registry = owner.directory / "groups"
            actual_status = owner.wait(timeout)
            if owner.authority.members():
                raise ReleaseCheckError(f"{name}: command left an active owned descendant")
            for nested in process_ownership.registered_commands(registry, exclude=owner.directory):
                status = nested.status()
                if status is None or status["phase"] != "released":
                    raise ReleaseCheckError(f"{name}: command left an active nested descendant")
                nested.stop(shutdown_timeout)
            for record, group in _registered_groups(registry):
                if _signal_group(group, 0):
                    raise ReleaseCheckError(f"{name}: command left an active nested descendant")
                record.unlink(missing_ok=True)
            # The actual child status is known, but the session anchor has not
            # exited or been reaped. Both ends verify closure before release.
            owner.release(shutdown_timeout)
        except BaseException as error:
            cleanup_errors = []
            deadline = time.monotonic() + shutdown_timeout
            if owner is not None:
                try:owner.stop(max(.001, deadline - time.monotonic()))
                except BaseException as cleanup_error:cleanup_errors.append(str(cleanup_error))
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if any(registry.rglob("*.pid")) or any(process_ownership.registered_commands(registry)):
                        raise ReleaseCheckError("Cleanup deadline; ownership retained")
                else:
                    _cleanup_registry(registry, remaining, exclude_session=owner.directory if owner is not None else None)
            except BaseException as cleanup_error:cleanup_errors.append(str(cleanup_error))
            print(_read(path), end="", flush=True)
            if cleanup_errors:
                recovery = f"; recovery record: {owner.directory / 'recovery.json'}" if owner is not None else ""
                raise ReleaseCheckError(f"{name}: {error}; cleanup failed: " + "; ".join(cleanup_errors) + recovery) from error
            if isinstance(error, process_ownership.OwnershipError):
                raise ReleaseCheckError(f"{name}: {error}") from error
            raise
    output = _read(path)
    print(output, end="" if output.endswith("\n") else "\n", flush=True)
    if actual_status:
        raise ReleaseCheckError(f"{name} exited with status {actual_status}")
    return output


def run_command(command: list[str], *, name: str, environment: dict[str, str],
                directory: Path, timeout: float, shutdown_timeout: float = 15) -> str:
    """Capture UTF-8 diagnostics and reap only this command's owned process tree."""
    if os.name == "posix" and sys.platform != "win32":
        return _run_posix_command(command, name=name, environment=environment,
                                  directory=directory, timeout=timeout, shutdown_timeout=shutdown_timeout)
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
            if job is not None and _job_still_active(job,shutdown_timeout):
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
            if ((self.job is not None and _job_still_active(self.job,timeout)) or
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


def modern_commands(directory, environment, *, research_python, swift):
    from tools.modernization.checks import commands
    return commands(ROOT,directory,environment,research_python=research_python,swift=swift)


def run_modern_checks(directory, environment, *, research_python, swift, command_timeout, shutdown_timeout):
    from tools.modernization.checks import count_tests
    path=directory/'modern-results.json'
    report={'profile':'modern','successful':False,'hardware_validation':'not_run',
            'swift_scope':'portable-contracts' if swift else 'not_requested','checks':[]}
    current=None
    try:
        if not research_python:
            raise ReleaseCheckError('--modern requires --research-python pointing to an isolated Python 3.15.1 environment')
        checks=modern_commands(directory,environment,research_python=research_python,swift=swift)
        if not checks or not any(check.kind in ('rust','node','python','swift','vitest') for check in checks):
            raise ReleaseCheckError('Modernization profile has no test suites')
        report['checks']=[{'name':check.name,'status':'not_run','tests_run':None} for check in checks]
        for check,current in zip(checks,report['checks']):
            current['status']='running'
            current['command']=check.command
            started=time.monotonic()
            try:
                child=environment.copy()
                child.update(check.environment)
                output=run_command(check.command,name=check.name,environment=child,directory=directory,
                                   timeout=command_timeout,shutdown_timeout=shutdown_timeout)
                if check.version and re.search(check.version,output,re.M) is None:
                    raise ReleaseCheckError(f'{check.name}: runtime version does not match the pinned toolchain')
                if check.kind=='swift':
                    validate_swift_output(output,check.name)
                current['tests_run']=count_tests(check.kind,output,check.report)
                current['status']='passed'
            finally:
                current['elapsed_seconds']=time.monotonic()-started
        report['successful']=True
    except BaseException as error:
        if current is not None:
            current['status']='failed'
        report['error']=f'{type(error).__name__}: {error}'
        if isinstance(error,ValueError):
            raise ReleaseCheckError(str(error)) from error
        raise
    finally:
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def _persist_release_report(directory: Path, report_dir: Path, *,
                            failure: BaseException | None, cleanup_errors: list[str],
                            swift: bool, modern: bool = False) -> None:
    """Keep diagnostics, never fixture databases, home directories or build trees."""
    report_dir.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="run-", dir=report_dir))
    for source in [*directory.glob("*.log"), directory / "python-results.json", directory / 'monitoring-endurance.json', *directory.glob("modern-*.json")]:
        if source.is_file():
            shutil.copy2(source, destination / source.name)
    summary = {
        "successful": failure is None and not cleanup_errors,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": sys.platform,
        "swift_requested": swift,
        "profile": "modern" if modern else "legacy",
        "hardware_validation": "not_run",
        "error": f"{type(failure).__name__}: {failure}" if failure else None,
        "cleanup_errors": list(cleanup_errors),
    }
    (destination / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Release diagnostics: {destination}", flush=True)


def _release_temporary_directory(*, modern: bool):
    if not modern or os.name != "posix":
        return tempfile.TemporaryDirectory(prefix="ori-release-checks-")
    # Reserve the Unix socket budget for children/.tmp.../candidate/.ori-runtime.
    # Canonical paths matter on macOS, where /var and /tmp are aliases.
    candidates = []
    try:
        candidates.append(tempfile.gettempdir())
    except (OSError, RuntimeError, UnicodeError):
        pass
    candidates.append("/tmp")
    for candidate in candidates:
        try:
            parent = Path(candidate).resolve(strict=True)
            if len(str(parent).encode("utf-8")) <= 16 and parent.is_dir():
                return tempfile.TemporaryDirectory(prefix="ori-release-checks-", dir=parent)
        except (OSError, RuntimeError, UnicodeError):
            continue
    raise ReleaseCheckError(
        "Modern POSIX checks require a canonical temporary parent of at most 16 UTF-8 bytes; "
        "the preferred temp directory and /tmp are unavailable or too long")


def release_checks(*, swift: bool = False, modern: bool = False, research_python: str | None = None, startup_timeout: float = 20,
                   command_timeout: float = 600, shutdown_timeout: float = 15,
                   report_dir: Path | None = None) -> None:
    environment = child_environment()
    environment.pop("ORI_RELEASE_PROCESS_REGISTRY", None)
    children: list[Fixture] = []
    failure: BaseException | None = None
    cleanup_errors = []
    with _release_temporary_directory(modern=modern) as temporary:
        # Canonicalize this owned root before passing it to children: macOS
        # aliases /var through /private/var, and Windows may return an 8.3 path.
        # Production writer ownership deliberately rejects symlink aliases.
        directory = Path(temporary).resolve()
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
        native_login_home = environment.get("HOME")
        environment["HOME"] = str(child_home)
        try:
            if modern:
                # Keep toolchain stores explicit before private HOME takes effect.
                for key,default in (("CARGO_HOME",Path.home()/".cargo"),("RUSTUP_HOME",Path.home()/".rustup")):
                    environment.setdefault(key,str(default))
                environment.update(CARGO_BUILD_JOBS="2",RUST_TEST_THREADS="2",GOMAXPROCS="2",RAYON_NUM_THREADS="2",UV_THREADPOOL_SIZE="2",CARGO_TARGET_DIR=str(directory/"modern-rust-target"),
                                   OPENBLAS_NUM_THREADS="1",OMP_NUM_THREADS="1",MKL_NUM_THREADS="1",VECLIB_MAXIMUM_THREADS="1")
                run_modern_checks(directory,environment,research_python=research_python,swift=swift,
                                  command_timeout=command_timeout,shutdown_timeout=shutdown_timeout)
            else:
                if swift and sys.platform != "darwin":
                    raise ReleaseCheckError("--swift requires macOS with Xcode")
                if swift and (shutil.which("swift") is None or shutil.which("xcodebuild") is None):
                    raise ReleaseCheckError("--swift requires swift and xcodebuild on PATH")
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
                endurance_path=directory/'monitoring-endurance.json'
                run_command([sys.executable,'-m','tests.monitoring_endurance','--report',str(endurance_path)],
                    name='monitoring-seven-day',environment=environment,directory=directory,
                    timeout=command_timeout,shutdown_timeout=shutdown_timeout)
                endurance=json.loads(endurance_path.read_text(encoding='utf-8'))
                if not (endurance.get('logical_elapsed_seconds',0)>=604800 and endurance.get('raw_sweeps',0)>0
                        and endurance.get('raw_sweeps')==endurance.get('raw_verified')
                        and endurance.get('bounded_working_registry') and endurance.get('restart_no_duplicate_events')
                        and endurance.get('active_workers')==0 and endurance.get('hardware_validation')=='not_run'
                        and endurance.get('comparison',{}).get('comparable_weak_recall')):
                    raise ReleaseCheckError('Seven-day synthetic monitoring acceptance did not pass')
                if swift:
                    for name, package in (("swift-sdk", "macos/SpektrKomandy"), ("swift-native", "macos")):
                        command = ["swift", "test", "--package-path", package,
                                   "--scratch-path", str(directory / (name + "-build"))]
                        command_environment = environment
                        if name == "swift-native":
                            # This explicit hardware probe belongs to bench acceptance;
                            # synthetic runs must never auto-open an attached device.
                            command += ["--skip", "DvoynoyObzorSessionTests.SessionTests/testPhysicalTinySAUltraSmokeIfConnected"]
                            # Native Security/WebKit need the actual login context;
                            # keep private temp roots and the final fixture URLs.
                            command_environment = environment.copy()
                            if native_login_home is None:
                                command_environment.pop("HOME", None)
                            else:
                                command_environment["HOME"] = native_login_home
                        output = run_command(command,
                                             name=name, environment=command_environment, directory=directory,
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
            if report_dir is not None:
                try:
                    _persist_release_report(directory, Path(report_dir), failure=failure,
                                            cleanup_errors=cleanup_errors, swift=swift, modern=modern)
                except (OSError, ValueError) as error:
                    cleanup_errors.append(f"Cannot save release diagnostics: {error}")
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


def _windows_supervisor_exit_status(returncode: int) -> int:
    """Preserve a child DWORD through CPython's signed C-long SystemExit path."""
    if type(returncode) is not int or not 0 <= returncode <= 0xffffffff:
        raise ValueError("Windows child exit status must be a DWORD")
    print(f"[release-child-exit] decimal={returncode} hex=0x{returncode:08X}",
          file=sys.stderr, flush=True)
    return returncode if returncode < 0x80000000 else returncode - 0x100000000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swift", action="store_true", help="Run native macOS/Xcode checks, or portable contracts with --modern")
    parser.add_argument("--modern",action="store_true",help="Select modernization checks; full release also requires the legacy profile")
    parser.add_argument("--research-python",help="Explicit isolated Python 3.15.1 interpreter for --modern")
    parser.add_argument("--startup-timeout", type=positive_seconds, default=20, metavar="SECONDS")
    parser.add_argument("--command-timeout", type=positive_seconds, default=600, metavar="SECONDS")
    parser.add_argument("--shutdown-timeout", type=positive_seconds, default=15, metavar="SECONDS")
    parser.add_argument("--report-dir", type=Path, help="Preserve logs and JSON results, including failures")
    parser.add_argument("--_python-suite", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_single-test", help=argparse.SUPPRESS)
    parser.add_argument("--_dependency-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_diagnostic-after", type=positive_seconds, default=60, help=argparse.SUPPRESS)
    parser.add_argument("--_supervise-command", help=argparse.SUPPRESS)
    parser.add_argument("--_session-owner", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.research_python and not args.modern:
        parser.error("--research-python requires --modern")
    if args._single_test is not None and args._python_suite is None:
        parser.error("--_single-test requires --_python-suite")
    if args._session_owner is not None and (args._supervise_command is None or os.name != "posix"):
        parser.error("--_session-owner requires a private POSIX command")
    if args._supervise_command is not None:
        if sys.stdin.buffer.readline() != b"go\n":
            return 1
        command = json.loads(args._supervise_command)
        if args._session_owner is not None:
            return process_ownership.supervise_anchor(command, args._session_owner, args.shutdown_timeout,
                lambda registry, budget, exclude: _cleanup_registry(registry, budget, exclude_session=exclude))
        if os.name == "posix":
            os.execvpe(command[0], command, os.environ)
        return _windows_supervisor_exit_status(subprocess.call(command))
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
        release_checks(swift=args.swift,modern=args.modern,research_python=args.research_python,startup_timeout=args.startup_timeout,
                       command_timeout=args.command_timeout, shutdown_timeout=args.shutdown_timeout,
                       report_dir=args.report_dir)
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
