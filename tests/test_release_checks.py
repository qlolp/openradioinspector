"""Release orchestrator regression tests, using only private fake children."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import run_release_checks as release


BEFORE = "a" * 64
READY = {"url": "http://127.0.0.1:49152/platform", "dump_sha256_before": BEFORE}
AFTER = {"dump_sha256_after": BEFORE, "unchanged": True}


class ReleaseGateTests(unittest.TestCase):
    def test_child_environment_replaces_smoke_optins_without_mutating_parent(self):
        source = {"ORI_OTHER_SMOKE_BASE_URL": "https://do-not-contact.invalid",
                  "ORI_AVERAGE_SMOKE_BASE_URL": "https://do-not-contact.invalid",
                  "THREE_THRESHOLDS_SMOKE_URL": "https://do-not-contact.invalid",
                  "ORI_WINDOWS_RELEASE_TK": "0", "NO_PROXY": "example.invalid",
                  "PYTHONPATH": "/not-the-checkout"}
        with patch.dict(os.environ, source, clear=True):
            environment = release.child_environment()
            self.assertEqual(dict(os.environ), source)
        self.assertNotIn("ORI_OTHER_SMOKE_BASE_URL", environment)
        self.assertNotIn("ORI_AVERAGE_SMOKE_BASE_URL", environment)
        self.assertNotIn("THREE_THRESHOLDS_SMOKE_URL", environment)
        self.assertEqual(environment["ORI_WINDOWS_RELEASE_TK"], "1")
        self.assertEqual(environment["ORI_ROUNDING_TK_SMOKE"], "1")
        self.assertEqual(environment["PYTHONPATH"], str(release.ROOT))
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        self.assertIn("127.0.0.1", environment["NO_PROXY"])
        self.assertIn("127.0.0.1", environment["no_proxy"])

    def test_only_existing_windows_test_identities_and_reasons_are_allowed(self):
        allowed = [(name + ".test_synthetic", reason)
                   for name, reason in release.WINDOWS_SKIPPED_CLASSES.items()]
        allowed.extend(release.WINDOWS_SKIPPED_TESTS.items())
        unexpected = [("tests.test_live.LiveTests.test_http", "POSIX pseudo terminals required"),
                      (allowed[0][0], "Optional synthetic core HTTP fixture"),
                      ("tests.test_windows_release_flows.WindowsReleaseFlowTests.test_form", "Opt-in hidden Tk desktop test")]
        self.assertEqual(release.unexpected_skips(allowed, platform="nt"), [])
        self.assertEqual(release.unexpected_skips(allowed + unexpected, platform="nt"), unexpected)
        self.assertEqual(release.unexpected_skips(allowed, platform="posix"), allowed)

    def test_python_suite_records_and_rejects_unexpected_skips(self):
        class Skipped(unittest.TestCase):
            @unittest.skip("Optional synthetic HTTP fixture")
            def test_missing_fixture(self):
                pass
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            with patch.object(release.unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite([Skipped("test_missing_fixture")])), redirect_stderr(io.StringIO()):
                self.assertEqual(release.run_python_suite(path), 1)
            report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["tests_run"], 1)
        self.assertEqual(len(report["unexpected_skips"]), 1)

    def test_python_suite_rejects_empty_discovery(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(release.unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite()), redirect_stderr(io.StringIO()):
            self.assertEqual(release.run_python_suite(Path(directory) / "empty.json"), 1)

    def test_missing_node_is_fatal_before_imports_or_tk(self):
        with patch.object(release.shutil, "which", return_value=None), patch.object(release.importlib, "import_module") as imports:
            with self.assertRaisesRegex(release.ReleaseCheckError, "Node is required"):
                release.check_dependencies()
            imports.assert_not_called()

    def test_missing_numpy_serial_or_tk_is_fatal(self):
        for missing in ("numpy", "serial", "tkinter"):
            with self.subTest(missing=missing), patch.object(release.shutil, "which", return_value="node"):
                def import_module(name):
                    if name == missing:
                        raise ImportError("not installed")
                with patch.object(release.importlib, "import_module", side_effect=import_module), self.assertRaisesRegex(release.ReleaseCheckError, missing):
                    release.check_dependencies()

    def test_tk_preflight_uses_only_own_hidden_window_and_destroys_it(self):
        window = Mock()
        fake = SimpleNamespace(Tk=Mock(return_value=window), TclError=RuntimeError)
        with patch.object(release.shutil, "which", return_value="node"), patch.object(release.importlib, "import_module"), patch.dict(sys.modules, {"tkinter": fake}), redirect_stdout(io.StringIO()):
            release.check_dependencies()
        window.withdraw.assert_called_once_with()
        window.destroy.assert_called_once_with()

    def test_tk_display_failure_is_not_a_skip(self):
        fake = SimpleNamespace(Tk=Mock(side_effect=RuntimeError("no display")), TclError=RuntimeError)
        with patch.object(release.shutil, "which", return_value="node"), patch.object(release.importlib, "import_module"), patch.dict(sys.modules, {"tkinter": fake}), self.assertRaisesRegex(release.ReleaseCheckError, "xvfb-run"):
            release.check_dependencies()

    def test_swift_skips_and_missing_tests_are_fatal(self):
        release.validate_swift_output("Executed 120 tests, with 0 failures\n", "sdk")
        release.validate_swift_output("Executed 120 tests, with 0 tests skipped and 0 failures\n", "sdk")
        for output in ("Test Case 'SyntheticTests.testLive' skipped (0.1 seconds)\nExecuted 1 test",
                       "Executed 120 tests, with 1 test skipped and 0 failures",
                       "Executed 0 tests, with 0 failures", "Build complete!"):
            with self.subTest(output=output), self.assertRaises(release.ReleaseCheckError):
                release.validate_swift_output(output, "sdk")

    def test_swift_on_non_macos_fails_before_spawning(self):
        with patch.object(release.sys, "platform", "win32"), patch.object(release, "run_command") as command, self.assertRaisesRegex(release.ReleaseCheckError, "requires macOS"):
            release.release_checks(swift=True)
        command.assert_not_called()

    def test_nonpositive_and_nonfinite_timeout_rejected(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(Exception):
                release.positive_seconds(value)


class ReleaseDiagnosticTests(unittest.TestCase):
    def test_windows_supervisor_preserves_dword_bits_and_logs_original_status(self):
        for raw, signed in ((0, 0), (9, 9), (0x7fffffff, 0x7fffffff),
                            (0x80000000, -0x80000000), (0xc0000005, -1073741819),
                            (0xffffffff, -1)):
            with self.subTest(raw=raw), redirect_stderr(io.StringIO()) as output:
                self.assertEqual(release._windows_supervisor_exit_status(raw), signed)
                self.assertEqual(signed & 0xffffffff, raw)
                self.assertEqual(output.getvalue(),
                                 f"[release-child-exit] decimal={raw} hex=0x{raw:08X}\n")
        for invalid in (-1, 0x100000000, True, 1.0, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                release._windows_supervisor_exit_status(invalid)


    def test_windows_supervisor_waits_for_assignment_and_propagates_child_status(self):
        command = ["owned-python", "-c", "synthetic"]
        for gate in (b"", b"wrong\n", b"go\n"):
            with self.subTest(gate=gate), patch.object(release, "os", SimpleNamespace(name="nt")), patch.object(release.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(gate))), patch.object(release.subprocess, "call", return_value=0xc0000005) as call, redirect_stderr(io.StringIO()) as output:
                result = release.main(["--_supervise-command", json.dumps(command)])
                if gate == b"go\n":
                    self.assertEqual(result, -1073741819)
                    call.assert_called_once_with(command)
                    self.assertIn("decimal=3221225477 hex=0xC0000005", output.getvalue())
                else:
                    self.assertEqual(result, 1)
                    call.assert_not_called()
                    self.assertEqual(output.getvalue(), "")


    def test_watchdog_version_boundaries_preserve_single_selection_and_cleanup(self):
        for version, native in (((3, 12, 10), False), ((3, 13, 9), False),
                                ((3, 13, 10), True), ((3, 14, 0), False),
                                ((3, 14, 1), True)):
            for outcome in (0, 1, KeyboardInterrupt(), RuntimeError("suite failed")):
                with self.subTest(version=version, outcome=outcome), patch.object(release.sys, "version_info", version), patch.object(release, "run_python_suite") as suite, patch.object(release, "_GilSafeWatchdog") as watchdog, patch.object(release.faulthandler, "enable") as enable, patch.object(release.faulthandler, "dump_traceback_later") as arm, patch.object(release.faulthandler, "cancel_dump_traceback_later") as cancel:
                    if isinstance(outcome, BaseException):
                        suite.side_effect = outcome
                        with self.assertRaises(type(outcome)):
                            release._run_diagnostic_python_suite(Path("unused.json"), 60, only_test="module.Case.test_one")
                    else:
                        suite.return_value = outcome
                        self.assertEqual(release._run_diagnostic_python_suite(Path("unused.json"), 60, only_test="module.Case.test_one"), outcome)
                    suite.assert_called_once_with(Path("unused.json"), diagnostics=True, only_test="module.Case.test_one")
                    enable.assert_called_once_with(file=sys.__stderr__, all_threads=True)
                    if native:
                        watchdog.assert_not_called()
                        arm.assert_called_once_with(60, repeat=True, file=sys.__stderr__, exit=False)
                        cancel.assert_called_once_with()
                    else:
                        arm.assert_not_called()
                        cancel.assert_not_called()
                        watchdog.assert_called_once_with(60, sys.__stderr__)
                        watchdog.return_value.start.assert_called_once_with()
                        watchdog.return_value.close.assert_called_once_with()


    def test_gil_watchdog_repeats_and_records_dump_failure(self):
        stream = io.StringIO()
        watchdog = release._GilSafeWatchdog(60, stream)
        watchdog.stopped = Mock()
        watchdog.stopped.wait.side_effect = [False, False, True]
        with patch.object(release.faulthandler, "dump_traceback") as dump:
            watchdog._run()
        self.assertEqual(dump.call_count, 2)
        dump.assert_called_with(file=stream, all_threads=True)
        self.assertEqual(stream.getvalue().count("GIL-safe diagnostic watchdog"), 2)
        failure = RuntimeError("dump failed")
        watchdog.stopped.wait.side_effect = [False]
        with patch.object(release.faulthandler, "dump_traceback", side_effect=failure):
            watchdog._run()
        self.assertIs(watchdog.error, failure)


    def test_gil_watchdog_join_is_bounded_and_errors_fail_closed(self):
        for alive, error in ((False, None), (True, None), (False, RuntimeError("dump failed"))):
            with self.subTest(alive=alive, error=error):
                watchdog = release._GilSafeWatchdog(60, io.StringIO())
                watchdog.thread = Mock()
                watchdog.thread.is_alive.return_value = alive
                watchdog.error = error
                if alive or error:
                    with self.assertRaises(release.ReleaseCheckError):
                        watchdog.close()
                else:
                    watchdog.close()
                self.assertTrue(watchdog.stopped.is_set())
                watchdog.thread.join.assert_called_once_with(timeout=2)


    def test_gil_watchdog_real_thread_dumps_and_joins(self):
        dumped = threading.Event()
        watchdog = release._GilSafeWatchdog(0.01, io.StringIO())
        with patch.object(release.faulthandler, "dump_traceback", side_effect=lambda **kwargs: dumped.set()):
            watchdog.start()
            try:
                self.assertTrue(dumped.wait(2), "owned watchdog did not dump")
            finally:
                watchdog.close()
        self.assertFalse(watchdog.thread.is_alive())


    def test_gil_watchdog_close_failure_cannot_turn_suite_into_success(self):
        with patch.object(release.sys, "version_info", (3, 12, 10)), patch.object(release.faulthandler, "enable"), patch.object(release, "run_python_suite", return_value=0), patch.object(release, "_GilSafeWatchdog") as watchdog:
            watchdog.return_value.close.side_effect = release.ReleaseCheckError("watchdog failed")
            with self.assertRaisesRegex(release.ReleaseCheckError, "watchdog failed"):
                release._run_diagnostic_python_suite(Path("unused.json"), 60)


    def test_nested_plain_suite_never_changes_process_global_watchdog(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(release.unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite()), patch.object(release.faulthandler, "enable") as enable, patch.object(release.faulthandler, "dump_traceback_later") as arm, patch.object(release.faulthandler, "cancel_dump_traceback_later") as cancel, redirect_stderr(io.StringIO()):
            release.run_python_suite(Path(directory) / "nested.json")
        arm.assert_not_called()
        cancel.assert_not_called()
        enable.assert_not_called()

    def test_cli_watchdog_is_periodic_nonterminating_and_cancelled_after_every_outcome(self):
        for outcome in (0, 1, KeyboardInterrupt(), RuntimeError("synthetic failure")):
            with self.subTest(outcome=outcome), patch.object(release.sys, "version_info", (3, 14, 1)), patch.object(release, "run_python_suite") as suite, patch.object(release.faulthandler, "enable") as enable, patch.object(release.faulthandler, "dump_traceback_later") as arm, patch.object(release.faulthandler, "cancel_dump_traceback_later") as cancel:
                if isinstance(outcome, BaseException):
                    suite.side_effect = outcome
                    with self.assertRaises(type(outcome)):
                        release._run_diagnostic_python_suite(Path("unused.json"), 0.1)
                else:
                    suite.return_value = outcome
                    self.assertEqual(release._run_diagnostic_python_suite(Path("unused.json"), 0.1), outcome)
                arm.assert_called_once_with(0.1, repeat=True, file=sys.__stderr__, exit=False)
                enable.assert_called_once_with(file=sys.__stderr__, all_threads=True)
                cancel.assert_called_once_with()
                suite.assert_called_once_with(Path("unused.json"), diagnostics=True)

    def test_legacy_python_uses_joined_gil_safe_watchdog_without_c_timer(self):
        import threading
        existing_threads = set(threading.enumerate())
        for outcome in (0, RuntimeError("synthetic failure")):
            dumped = threading.Event()
            def run(*args, **kwargs):
                self.assertTrue(dumped.wait(1), "watchdog did not run")
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            with self.subTest(outcome=outcome), patch.object(release.sys, "version_info", (3, 12, 10)), patch.object(release, "run_python_suite", side_effect=run), patch.object(release.faulthandler, "dump_traceback_later") as arm, patch.object(release.faulthandler, "dump_traceback", side_effect=lambda **kwargs: dumped.set()) as dump:
                if isinstance(outcome, BaseException):
                    with self.assertRaises(type(outcome)):
                        release._run_diagnostic_python_suite(Path("unused.json"), 0.01)
                else:
                    self.assertEqual(release._run_diagnostic_python_suite(Path("unused.json"), 0.01), 0)
                arm.assert_not_called()
                dump.assert_called()
                self.assertFalse(any(t.name == "ori-diagnostic-watchdog" and t not in existing_threads
                                     for t in threading.enumerate()))

    def test_test_boundaries_include_utc_exact_id_elapsed_and_duration(self):
        output = io.StringIO()
        runner = unittest.TextTestRunner(stream=output, verbosity=2, resultclass=release.DiagnosticTestResult)
        case = unittest.FunctionTestCase(lambda: None)
        with patch.object(release.time, "monotonic", side_effect=[10.0, 12.0, 15.0]):
            result = runner._makeResult()
            result.startTest(case)
            result.addSuccess(case)
            result.stopTest(case)
        text = output.getvalue()
        self.assertIn("[release-test-start]", text)
        self.assertIn("elapsed=2.000s", text)
        self.assertIn("elapsed=5.000s duration=3.000s", text)
        self.assertIn("id=" + case.id(), text)
        self.assertRegex(text, r"utc=\d{4}-\d\d-\d\dT[^ ]+\+00:00")

    def test_preflight_reports_versions_from_existing_owned_root_only(self):
        calls = []
        def call(*args):
            calls.append(args)
            return {("package", "provide", "Tk"): "8.6.13", ("info", "patchlevel"): "8.6.13",
                    ("tk", "windowingsystem"): "aqua"}[args]
        root = SimpleNamespace(tk=SimpleNamespace(call=call), withdraw=Mock(), update_idletasks=Mock(), destroy=Mock())
        module = SimpleNamespace(Tk=Mock(return_value=root), TclError=RuntimeError)
        with patch.object(release.shutil, "which", return_value="node"), patch.object(release.importlib, "import_module"), patch.dict(sys.modules, {"tkinter": module}), redirect_stdout(io.StringIO()) as output:
            release.check_dependencies()
        self.assertIn("Python=" + sys.version.split()[0], output.getvalue())
        self.assertEqual(output.getvalue().count("Tcl=8.6.13 Tk=8.6.13 windowing=aqua"), 1)
        module.Tk.assert_called_once_with()
        root.destroy.assert_called_once_with()
        self.assertTrue(all(command in (("package", "provide", "Tk"), ("info", "patchlevel"), ("tk", "windowingsystem")) for command in calls))


class OwnedProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ori-runner-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.environment = release.child_environment()
        self.environment.update({key: str(self.directory) for key in ("TMPDIR", "TEMP", "TMP")})

    def fixture(self, body: str, name="synthetic") -> release.Fixture:
        fixture = release.Fixture(name, self.directory, self.environment,
                                  command=[sys.executable, "-c", body])
        def reap():
            if fixture.process.poll() is None:
                fixture.process.kill()
            fixture.process.wait(timeout=5)
            if fixture.process.stdin:
                fixture.process.stdin.close()
            fixture.log.close()
            fixture.stderr.close()
        self.addCleanup(reap)
        return fixture

    def good_body(self, final=None):
        return ("import json,sys; "
                f"print(json.dumps({READY!r}), flush=True); "
                "sys.stdin.buffer.read(); "
                f"print(json.dumps({AFTER if final is None else final!r}), flush=True)")

    def test_start_and_eof_shutdown_verify_exact_digest_and_utf8(self):
        fixture = self.fixture(self.good_body())
        self.assertEqual(fixture.ready(5), "http://127.0.0.1:49152")
        with redirect_stdout(io.StringIO()) as output:
            fixture.close(5)
        self.assertEqual(fixture.process.returncode, 0)
        self.assertIn("archive unchanged", output.getvalue())
        self.assertTrue(fixture.log.closed)

    def test_windows_job_accounting_can_settle_after_clean_process_exit(self):
        fixture=self.fixture(self.good_body());fixture.ready(5)
        if fixture.job is not None:self.addCleanup(fixture.job.close)
        fixture.job=Mock()
        # Windows signals the parent handle before job accounting necessarily
        # removes its last process. No live descendant exists in this fixture.
        fixture.job.active.side_effect=[1,0]
        with patch.object(release,'_stop_command') as stop,redirect_stdout(io.StringIO()) as output:
            fixture.close(.1)
        stop.assert_not_called()
        self.assertIn('archive unchanged',output.getvalue())

    def test_windows_job_that_remains_active_still_fails_closed(self):
        fixture=self.fixture(self.good_body());fixture.ready(5)
        if fixture.job is not None:self.addCleanup(fixture.job.close)
        fixture.job=Mock();fixture.job.active.return_value=1
        with patch.object(release,'_stop_command') as stop,redirect_stdout(io.StringIO()),self.assertRaisesRegex(release.ReleaseCheckError,'forced cleanup'):
            fixture.close(.02)
        # wait(timeout) may already force one stop before the still-active job
        # gate fires another; fail-closed only requires at least one stop.
        self.assertGreaterEqual(stop.call_count, 1)

    def test_startup_exit_timeout_bad_json_and_nonlocal_url_fail(self):
        bodies = ["raise SystemExit(7)", "import time; time.sleep(30)",
                  "import sys; print('not json', flush=True); sys.stdin.read()",
                  "import sys; print('[]', flush=True); sys.stdin.read()",
                  "import sys; print(" + repr(json.dumps({**READY, "url": "http://example.invalid"})) + ", flush=True); sys.stdin.read()"]
        for number, body in enumerate(bodies):
            with self.subTest(number=number):
                fixture = self.fixture(body, f"bad-{number}")
                with self.assertRaises(release.ReleaseCheckError):
                    fixture.ready(0.15 if number == 1 else 5)
                with self.assertRaises(release.ReleaseCheckError):
                    fixture.close(0.15)
                self.assertIsNotNone(fixture.process.poll())

    def test_mismatched_hash_false_flag_missing_record_and_nonzero_exit_fail_cleanup(self):
        for number, final in enumerate(({**AFTER, "dump_sha256_after": "b" * 64},
                                        {**AFTER, "unchanged": False}, {}, [])):
            with self.subTest(final=final):
                fixture = self.fixture(self.good_body(final), f"mismatch-{number}")
                fixture.ready(5)
                with self.assertRaisesRegex(release.ReleaseCheckError, "verification failed"):
                    fixture.close(5)
        fixture = self.fixture(self.good_body() + "; sys.exit(2)", "nonzero")
        fixture.ready(5)
        with self.assertRaises(release.ReleaseCheckError):
            fixture.close(5)

    def test_timeout_kills_only_owned_fixture_and_never_claims_archive_unchanged(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: unrelated.wait(timeout=5))
        self.addCleanup(unrelated.stdin.close)
        fixture = self.fixture(self.good_body() + "; import time; time.sleep(30)")
        fixture.ready(5)
        with redirect_stdout(io.StringIO()) as output, self.assertRaisesRegex(release.ReleaseCheckError, "forced cleanup; archive integrity unverified"):
            fixture.close(0.15)
        self.assertEqual(output.getvalue(), "")
        self.assertIsNone(unrelated.poll())
        self.assertIsNotNone(fixture.process.poll())

    def test_test_command_failure_and_timeout_are_not_success(self):
        with redirect_stdout(io.StringIO()) as output, self.assertRaisesRegex(release.ReleaseCheckError, "status 9"):
            release.run_command([sys.executable, "-c", "print('проверка'); raise SystemExit(9)"],
                                name="failed", environment=self.environment, directory=self.directory, timeout=5)
        self.assertIn("проверка", output.getvalue())
        if sys.platform == "win32":
            # Exit only our owned synthetic child, without generating a crash dump.
            for status in (0x80000000, 0xc0000005, 0xffffffff):
                body = ("import ctypes; from ctypes import wintypes; "
                        "exit_process = ctypes.WinDLL('kernel32').ExitProcess; "
                        "exit_process.argtypes = [wintypes.UINT]; "
                        "exit_process.restype = None; "
                        f"exit_process({status})")
                with self.subTest(status=status), redirect_stdout(io.StringIO()) as output, self.assertRaisesRegex(release.ReleaseCheckError, f"status {status}$"):
                    release.run_command([sys.executable, "-c", body], name="dword-exit",
                                        environment=self.environment, directory=self.directory,
                                        timeout=5, shutdown_timeout=2)
                self.assertIn(f"decimal={status} hex=0x{status:08X}", output.getvalue())
        with redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            release.run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                                name="timeout", environment=self.environment, directory=self.directory,
                                timeout=0.15, shutdown_timeout=2)

    def test_real_watchdog_dumps_owned_python_stack_then_cancels(self):
        body = """
from pathlib import Path
import time
from unittest.mock import patch
import sys
from tests import run_release_checks as release
trace = Path(sys.argv[1])
def synthetic_blocked_test(*args, **kwargs):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if 'synthetic_blocked_test' in trace.read_text():
            return 0
        time.sleep(0.005)
    raise AssertionError('Owned watchdog did not dump the blocked frame')
release.run_python_suite = synthetic_blocked_test
with trace.open('w') as log, patch.object(release.sys, '__stderr__', log):
    assert release._run_diagnostic_python_suite(Path('unused.json'), 0.03) == 0
    before = trace.read_text()
    print(before, flush=True)
    print('WATCHDOG_CANCELLED', flush=True)
    time.sleep(0.1)
    assert trace.read_text() == before, 'Watchdog continued after cancellation'
"""
        # This case tests the watchdog, not nested Job Object teardown. Its
        # one Python child inherits the outer command's ownership; it spawns no
        # other processes. Capture errors even when the child exits abnormally.
        completed = subprocess.run([sys.executable, "-c", body, str(self.directory/'owned-watchdog.log')],
                                   cwd=release.ROOT, env=self.environment,
                                   capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        output = completed.stdout + completed.stderr
        before, after = output.split("WATCHDOG_CANCELLED", 1)
        self.assertIn("Timeout", before)
        self.assertIn("synthetic_blocked_test", before)
        self.assertNotIn("Timeout", after)

    def test_interrupted_command_is_reaped(self):
        process = Mock()
        process.wait.side_effect = [KeyboardInterrupt, None, None]
        process.poll.return_value = None
        with patch.object(release.subprocess, "Popen", return_value=process), patch.object(release, "_signal_group", side_effect=lambda _process, signum: signum != 0) as group, patch.object(release.sys, "platform", "darwin"), redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            release.run_command(["synthetic-command"], name="interrupted", environment=self.environment,
                                directory=self.directory, timeout=1)
        self.assertEqual(group.call_args_list[0].args, (process, release.signal.SIGTERM))
        process.kill.assert_not_called()

    def test_timed_out_command_descendant_is_stopped_but_unrelated_child_survives(self):
        ready = self.directory / "descendant-started"
        leaked = self.directory / "descendant-leaked"
        unrelated_done = self.directory / "unrelated-finished"
        descendant = (f"from pathlib import Path; import time; Path({str(ready)!r}).write_text('ready'); "
                      f"time.sleep(1.2); Path({str(leaked)!r}).write_text('leaked')")
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {descendant!r}]); time.sleep(30)"
        unrelated = subprocess.Popen([sys.executable, "-c", "from pathlib import Path; import time; time.sleep(1); "
                                      f"Path({str(unrelated_done)!r}).write_text('done')"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: unrelated.wait(timeout=5))
        with redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            release.run_command([sys.executable, "-c", parent], name="descendant-timeout",
                                environment=self.environment, directory=self.directory,
                                timeout=0.7, shutdown_timeout=2)
        self.assertTrue(ready.exists(), "the regression must actually start a descendant")
        time.sleep(1.3)
        self.assertFalse(leaked.exists(), "an owned descendant survived command cleanup")
        unrelated.wait(timeout=5)
        self.assertEqual(unrelated.returncode, 0)
        self.assertTrue(unrelated_done.exists())

    def test_outer_timeout_stops_registered_nested_session(self):
        ancestor = Path(tempfile.mkdtemp(prefix="ancestor-groups-", dir=self.environment.get("ORI_RELEASE_PROCESS_REGISTRY", self.directory)))
        self.environment["ORI_RELEASE_PROCESS_REGISTRY"] = str(ancestor)
        sibling = self.fixture(self.good_body(), "registered-sibling")
        sibling.ready(5)
        ready = self.directory / "nested-started"
        leaked = self.directory / "nested-leaked"
        child = (f"from pathlib import Path; import time; Path({str(ready)!r}).write_text('ready'); "
                 f"time.sleep(1.4); Path({str(leaked)!r}).write_text('leaked')")
        parent = ("from tests.run_release_checks import run_command; from pathlib import Path; import os,sys; "
                  f"run_command([sys.executable, '-c', {child!r}], name='inner', environment=dict(os.environ), "
                  f"directory=Path({str(self.directory)!r}), timeout=30)")
        # Exercise a real wait timeout after the nested session has started.
        # The old 0.8s launch budget raced four Python/supervisor startups under
        # load and sometimes never reached the cleanup scenario being tested.
        original_wait = subprocess.Popen.wait
        def wait_after_nested_start(process, timeout=None):
            if timeout == 10:
                deadline = time.monotonic() + 8
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(ready.exists(), 'nested child did not start')
                return original_wait(process, timeout=0.05)
            return original_wait(process, timeout=timeout)
        with patch.object(subprocess.Popen, 'wait', wait_after_nested_start), redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            release.run_command([sys.executable, "-c", parent], name="outer-timeout",
                                environment=self.environment, directory=self.directory,
                                timeout=10, shutdown_timeout=2)
        self.assertTrue(ready.exists())
        time.sleep(1.5)
        self.assertFalse(leaked.exists(), "a detached nested session outlived its owner")
        self.assertIsNone(sibling.process.poll(), "nested cleanup stopped a sibling's group")
        with redirect_stdout(io.StringIO()):
            sibling.close(5)

    def test_successful_command_with_running_descendant_fails_and_cleans_it(self):
        ready = self.directory / "success-child-started"
        child = f"from pathlib import Path; import time; Path({str(ready)!r}).write_text('ready'); time.sleep(30)"
        parent = (f"import subprocess,sys,time; from pathlib import Path; subprocess.Popen([sys.executable, '-c', {child!r}]); "
                  f"\nwhile not Path({str(ready)!r}).exists(): time.sleep(0.01)")
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(release.ReleaseCheckError, "active owned descendant"):
            release.run_command([sys.executable, "-c", parent], name="leaked-success",
                                environment=self.environment, directory=self.directory,
                                timeout=5, shutdown_timeout=2)

    def test_windows_supervisor_cannot_start_before_job_assignment(self):
        events = []
        job = Mock()
        job.active.return_value = 0
        process = Mock()
        process.returncode = 0
        job.assign.side_effect = lambda _process: events.append("assign")
        process.stdin.write.side_effect = lambda _data: events.append("start")
        with patch.object(release.sys, "platform", "win32"), patch.object(release, "WindowsJob", return_value=job), patch.object(release, "_process_options", return_value={}), patch.object(release.subprocess, "Popen", return_value=process) as popen, redirect_stdout(io.StringIO()):
            release.run_command(["synthetic-command"], name="windows-gate", environment=self.environment,
                                directory=self.directory, timeout=1)
        self.assertEqual(events, ["assign", "start"])
        self.assertIn("--_supervise-command", popen.call_args.args[0])
        process.stdin.close.assert_called_once_with()
        job.close.assert_called_once_with()

    def test_posix_supervisor_cannot_start_before_group_registration(self):
        events = []
        process = Mock()
        process.returncode = 0
        process.stdin.write.side_effect = lambda _data: events.append("start")
        def register(*_args):
            events.append("register")
            return None
        with patch.object(release.sys, "platform", "darwin"), patch.object(release, "_register_group", side_effect=register), patch.object(release, "_signal_group", return_value=False), patch.object(release.subprocess, "Popen", return_value=process), redirect_stdout(io.StringIO()):
            release.run_command(["synthetic-command"], name="posix-gate", environment=self.environment,
                                directory=self.directory, timeout=1)
        self.assertEqual(events, ["register", "start"])

    def test_registration_is_retained_until_the_owned_group_is_gone(self):
        record = self.directory / "123.pid"
        record.write_text("123", encoding="ascii")
        process = Mock()
        process.poll.return_value = 0
        with patch.object(release, "_signal_group", return_value=True):
            release._retire_stopped_group(record, process)
        self.assertTrue(record.exists())
        with patch.object(release, "_signal_group", side_effect=PermissionError("unknown")):
            release._retire_stopped_group(record, process)
        self.assertTrue(record.exists())
        with patch.object(release, "_signal_group", return_value=False):
            release._retire_stopped_group(record, process)
        self.assertFalse(record.exists())

    def test_noncanonical_or_reserved_registry_targets_are_rejected(self):
        for name, contents in (("0.pid", "0"), ("1.pid", "1"), ("000123.pid", "000123"),
                               ("-123.pid", "-123"), ("123.pid", "456")):
            with self.subTest(name=name):
                record = self.directory / name
                record.write_text(contents, encoding="ascii")
                with self.assertRaisesRegex(release.ReleaseCheckError, "Invalid owned"):
                    list(release._registered_groups(self.directory))
                record.unlink()

    def test_windows_assignment_failure_never_releases_command_gate(self):
        job = Mock()
        job.assign.side_effect = OSError("job assignment denied")
        process = Mock()
        process.stdin.closed = False
        process.poll.return_value = None
        with patch.object(release.sys, "platform", "win32"), patch.object(release, "WindowsJob", return_value=job), patch.object(release, "_process_options", return_value={}), patch.object(release.subprocess, "Popen", return_value=process), redirect_stdout(io.StringIO()), self.assertRaisesRegex(OSError, "assignment denied"):
            release.run_command(["synthetic-command"], name="windows-failed-gate", environment=self.environment,
                                directory=self.directory, timeout=1)
        process.stdin.write.assert_not_called()
        process.terminate.assert_called_once_with()
        job.close.assert_called_once_with()

    def test_windows_timeout_terminates_only_owned_job(self):
        job = Mock()
        job.active.return_value = 0
        process = Mock()
        process.stdin.closed = True
        process.wait.side_effect = [subprocess.TimeoutExpired("owned", 1), None]
        with patch.object(release.sys, "platform", "win32"), patch.object(release, "WindowsJob", return_value=job), patch.object(release, "_process_options", return_value={}), patch.object(release.subprocess, "Popen", return_value=process), redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            release.run_command(["synthetic-command"], name="windows-timeout", environment=self.environment,
                                directory=self.directory, timeout=1)
        job.terminate.assert_called_once_with()
        job.close.assert_called_once_with()
        process.kill.assert_not_called()


class IsolatedTkHarnessTests(unittest.TestCase):
    case_id = "tests.test_windows_sweep_average.AverageActualTkHTTPTests.test_synthetic"

    def report(self, **changes):
        return {"tests_run": 1, "successful": True, "selected_test": self.case_id,
                "skipped": [], "unexpected_skips": [], **changes}

    def run_child(self, report, *, returncode=0, error=None):
        def child(command, **kwargs):
            self.assertEqual(command[command.index("--_single-test") + 1], self.case_id)
            self.assertEqual(command[command.index("--_diagnostic-after") + 1], "30")
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(kwargs["timeout"], 90)
            self.assertEqual(kwargs["cwd"], release.ROOT)
            for option in ("env", "start_new_session", "creationflags"):
                self.assertNotIn(option, kwargs)
            kwargs["stdout"].write("CHILD DIAGNOSTIC\n")
            if report is not None:
                Path(command[-1]).write_text(report if isinstance(report, str) else json.dumps(report))
            if error is not None:
                raise error
            return SimpleNamespace(returncode=returncode)
        result = unittest.TestResult()
        with patch.object(release.subprocess, "run", side_effect=child), redirect_stderr(io.StringIO()) as output:
            release.IsolatedTkTest(self.case_id).run(result)
        self.assertIn("CHILD DIAGNOSTIC", output.getvalue())
        self.assertEqual(result.testsRun, 1)
        return result

    def test_success_identity_count_and_child_contract(self):
        result = self.run_child(self.report())
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.skipped, [])
        self.assertEqual(release.IsolatedTkTest(self.case_id).id(), self.case_id)

    def test_failures_reports_and_exit_status_never_become_pass_or_skip(self):
        for report in (None, "not json", "[]", {}, self.report(tests_run=0),
                       self.report(tests_run=2), self.report(tests_run=True),
                       self.report(selected_test="wrong"), self.report(successful=False),
                       self.report(successful="true"), self.report(skipped=None),
                       self.report(skipped=[["wrong", "reason"]]),
                       self.report(skipped=[[self.case_id, "reason"]] * 2),
                       self.report(unexpected_skips=[[self.case_id, "reason"]]),
                       self.report(skipped=[[self.case_id, "forged allowed skip"]], unexpected_skips=[]),
                       self.report(skipped=[[self.case_id, "reason"]], unexpected_skips=[[self.case_id, "reason"]])):
            with self.subTest(report=report):
                result = self.run_child(report)
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(result.skipped, [])
        for status in (1, 9, -9):
            for skipped in ([], [[self.case_id, "reason"]]):
                with self.subTest(status=status, skipped=skipped):
                    result = self.run_child(self.report(skipped=skipped, unexpected_skips=skipped), returncode=status)
                    self.assertFalse(result.wasSuccessful())
                    self.assertEqual(result.skipped, [])
        result = self.run_child(self.report(), error=subprocess.TimeoutExpired("child", 90))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(result.skipped, [])

    def test_valid_allowed_skip_is_preserved_without_expanding_policy(self):
        with patch.object(release, "unexpected_skips", return_value=[]):
            result = self.run_child(self.report(skipped=[[self.case_id, "allowed reason"]]))
        self.assertEqual([(test.id(), reason) for test, reason in result.skipped],
                         [(self.case_id, "allowed reason")])
        result = self.run_child(self.report(skipped=[[self.case_id, "unexpected"]],
                                            unexpected_skips=[[self.case_id, "unexpected"]]), returncode=1)
        self.assertFalse(result.wasSuccessful())

    def test_only_five_named_classes_are_wrapped_and_lifecycle_is_unchanged(self):
        expected_classes = {
            "tests.test_windows_release_flows.WindowsReleaseFlowTests",
            "tests.test_windows_sweep_context.ActualTkHTTPTests",
            "tests.test_windows_three_thresholds.ActualTkHTTPTests",
            "tests.test_windows_frequency_rounding.ActualTkHTTPTests",
            "tests.test_windows_sweep_average.AverageActualTkHTTPTests",
        }
        self.assertEqual(release.MACOS_TK_CLASSES, expected_classes)
        cases = []
        for name in sorted(expected_classes):
            case = unittest.FunctionTestCase(lambda: None)
            case.id = lambda name=name: name + ".test_owned"
            cases.append(case)
        unchanged = []
        for name in ("tests.test_windows_tk_lifecycle.HiddenTkTimerLifecycleTests",
                     "tests.test_windows_tk_ownership.InterpreterOwnershipSubprocessTests"):
            case = unittest.FunctionTestCase(lambda: None)
            case.id = lambda name=name: name + ".test_owned"
            unchanged.append(case)
        suite = release.isolate_tk_cases(unittest.TestSuite([unittest.TestSuite(cases), *unchanged]))
        self.assertEqual(suite.countTestCases(), 7)
        self.assertEqual(list(release._case_ids(suite)), [case.id() for case in cases + unchanged])
        self.assertTrue(all(isinstance(case, release.IsolatedTkTest) for case in next(iter(suite))))
        for actual, original in zip(list(suite)[1:], unchanged):
            self.assertIs(actual, original)

    def test_single_selection_never_discovers_or_recursively_isolates(self):
        case = unittest.FunctionTestCase(lambda: None)
        case.id = lambda: self.case_id
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(release.unittest.defaultTestLoader, "loadTestsFromName", return_value=unittest.TestSuite([case])), \
                patch.object(release.unittest.defaultTestLoader, "discover") as discover, \
                patch.object(release, "isolate_tk_cases") as isolate, redirect_stderr(io.StringIO()):
            path = Path(directory) / "report.json"
            self.assertEqual(release.run_python_suite(path, only_test=self.case_id), 0)
            self.assertEqual(json.loads(path.read_text())["selected_test"], self.case_id)
            discover.assert_not_called()
            isolate.assert_not_called()

    def test_single_selection_rejects_empty_multiple_and_wrong_identity(self):
        case = unittest.FunctionTestCase(lambda: None)
        for cases in ([], [case, case], [case]):
            with self.subTest(count=len(cases)), \
                    patch.object(release.unittest.defaultTestLoader, "loadTestsFromName", return_value=unittest.TestSuite(cases)), \
                    self.assertRaisesRegex(release.ReleaseCheckError, "exactly"):
                release.run_python_suite(Path("unused.json"), only_test=self.case_id)

    def test_outer_timeout_retains_live_child_diagnostics_and_reaps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready, leaked = root / "ready", root / "leaked"
            child = ("import sys,time\nfrom pathlib import Path\n"
                     "from tests import run_release_checks as r\n"
                     "def synthetic_blocked_test(*args, **kwargs):\n"
                     f"    Path({str(ready)!r}).write_text('ready')\n"
                     "    print('CHILD_STDOUT', flush=True)\n"
                     "    print('CHILD_STDERR', file=sys.stderr, flush=True)\n"
                     "    time.sleep(1.5)\n"
                     f"    Path({str(leaked)!r}).write_text('leaked')\n"
                     "    return 0\n"
                     "r.run_python_suite = synthetic_blocked_test\n"
                     "r._run_diagnostic_python_suite(Path('unused.json'), 0.05)\n")
            parent = ("import subprocess,sys; from tests import run_release_checks as r; "
                      "original = subprocess.run; "
                      f"r.subprocess.run = lambda command, **kwargs: original([sys.executable, '-c', {child!r}], **kwargs); "
                      f"r.IsolatedTkTest({self.case_id!r}).runTest()")
            popen = subprocess.Popen
            def start(*args, **kwargs):
                process = popen(*args, **kwargs)
                wait = process.wait
                first_wait = True
                def wait_after_ready(*, timeout=None):
                    nonlocal first_wait
                    if first_wait:
                        first_wait = False
                        deadline = time.monotonic() + 20
                        while not ready.exists() and process.poll() is None:
                            if time.monotonic() >= deadline:
                                self.fail("synthetic child did not become ready within startup allowance")
                            time.sleep(0.01)
                        self.assertTrue(ready.exists(), "synthetic child exited before readiness")
                        return wait(timeout=0.3)
                    return wait(timeout=timeout)
                process.wait = wait_after_ready
                return process
            # Start the short timeout only once the real nested child is ready;
            # cold Windows/macOS Python imports get a separate startup allowance.
            with patch.object(release.subprocess, "Popen", side_effect=start), \
                    redirect_stdout(io.StringIO()) as output, self.assertRaises(subprocess.TimeoutExpired):
                release.run_command([sys.executable, "-c", parent], name="isolated-outer-timeout",
                                    environment=release.child_environment(), directory=root,
                                    timeout=20, shutdown_timeout=2)
            self.assertTrue(ready.exists())
            self.assertIn("CHILD_STDOUT", output.getvalue())
            self.assertIn("CHILD_STDERR", output.getvalue())
            self.assertIn("Timeout", output.getvalue())
            self.assertIn('File "<string>"', output.getvalue())
            time.sleep(1.6)
            self.assertFalse(leaked.exists())

    def test_single_cli_arms_watchdog_and_requires_report_path(self):
        with patch.object(release, "_run_diagnostic_python_suite", return_value=0) as run:
            self.assertEqual(release.main(["--_single-test", self.case_id, "--_python-suite", "owned.json"]), 0)
            run.assert_called_once_with(Path("owned.json"), 60, only_test=self.case_id)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            release.main(["--_single-test", self.case_id])


class OrchestrationTests(unittest.TestCase):
    def make_runner(self, *, failure=None, bad_cleanup=False):
        fixtures = []
        environments = []
        commands = []

        def fixture_factory(module, directory, environment):
            fixture = Mock()
            fixture.ready.return_value = f"http://127.0.0.1:{49000 + len(fixtures)}"
            if bad_cleanup:
                fixture.close.side_effect = release.ReleaseCheckError("archive mismatch")
            fixtures.append(fixture)
            environments.append(dict(environment))
            return fixture

        def command(command, **kwargs):
            commands.append((command, dict(kwargs["environment"])))
            self.assertTrue(Path(kwargs["environment"]["TMPDIR"]).is_dir())
            self.assertEqual(kwargs["environment"]["TMPDIR"], kwargs["environment"]["TEMP"])
            self.assertEqual(kwargs["environment"]["TMPDIR"], kwargs["environment"]["TMP"])
            self.assertTrue(Path(kwargs["environment"]["HOME"]).is_dir())
            self.assertEqual(list(Path(kwargs["environment"]["HOME"]).iterdir()), [])
            if "--_python-suite" in command:
                if failure is not None:
                    raise failure
                Path(command[-1]).write_text(json.dumps({"tests_run": 100, "successful": True,
                                                       "skipped": [], "unexpected_skips": []}), encoding="utf-8")
            if 'tests.monitoring_endurance' in command:
                Path(command[command.index('--report')+1]).write_text(json.dumps(dict(
                    logical_elapsed_seconds=604800,raw_sweeps=10045,raw_verified=10045,
                    bounded_working_registry=True,restart_no_duplicate_events=True,
                    active_workers=0,hardware_validation='not_run',comparison={'comparable_weak_recall':True})),encoding='utf-8')
            return "Executed 10 tests, with 0 failures\n"

        return fixtures, environments, commands, fixture_factory, command

    def test_every_fixture_optin_alias_and_private_temp_are_set_before_discovery(self):
        fixtures, environments, commands, factory, command = self.make_runner()
        with patch.object(release, "Fixture", side_effect=factory), patch.object(release, "run_command", side_effect=command), redirect_stdout(io.StringIO()):
            release.release_checks()
        self.assertEqual(len(fixtures), 7)
        test_environment = commands[-1][1]
        for key, _module in release.FIXTURES:
            self.assertTrue(test_environment[key].startswith("http://127.0.0.1:"))
        self.assertEqual(test_environment["THREE_THRESHOLDS_SMOKE_URL"], test_environment["ORI_THREE_THRESHOLDS_SMOKE_BASE_URL"])
        self.assertFalse(Path(test_environment["TMPDIR"]).exists())
        for fixture in fixtures:
            fixture.close.assert_called_once()

    def test_seven_day_acceptance_runs_inside_owned_release_environment(self):
        fixtures,environments,commands,factory,command=self.make_runner()
        with patch.object(release,'Fixture',side_effect=factory),patch.object(release,'run_command',side_effect=command),redirect_stdout(io.StringIO()):
            release.release_checks()
        endurance=[cmd for cmd,_ in commands if 'tests.monitoring_endurance' in cmd]
        self.assertEqual(len(endurance),1)
        self.assertIn('--report',endurance[0]);self.assertNotIn('--archive',endurance[0])

    def test_entrypoint_never_trusts_registry_from_callers_environment(self):
        _, environments, commands, factory, command = self.make_runner()
        with patch.dict(os.environ, {"ORI_RELEASE_PROCESS_REGISTRY": "/untrusted/caller/path"}), patch.object(release, "Fixture", side_effect=factory), patch.object(release, "run_command", side_effect=command), redirect_stdout(io.StringIO()):
            release.release_checks()
        for environment in [*environments, *(environment for _, environment in commands)]:
            self.assertNotIn("ORI_RELEASE_PROCESS_REGISTRY", environment)

    def test_command_failure_timeout_and_interrupt_close_every_started_fixture(self):
        for error in (release.ReleaseCheckError("test failed"), subprocess.TimeoutExpired("test", 1), KeyboardInterrupt()):
            fixtures, _, _, factory, command = self.make_runner(failure=error)
            with self.subTest(error=type(error).__name__), patch.object(release, "Fixture", side_effect=factory), patch.object(release, "run_command", side_effect=command), redirect_stdout(io.StringIO()), self.assertRaises(type(error)):
                release.release_checks()
            for fixture in fixtures:
                fixture.close.assert_called_once()

    def test_startup_failure_closes_all_started_children_including_failed_one(self):
        fixtures, _, _, factory, command = self.make_runner()
        def fail_second(*args):
            fixture = factory(*args)
            if len(fixtures) == 2:
                fixture.ready.side_effect = release.ReleaseCheckError("startup failure")
            return fixture
        with patch.object(release, "Fixture", side_effect=fail_second), patch.object(release, "run_command", side_effect=command), redirect_stdout(io.StringIO()), self.assertRaisesRegex(release.ReleaseCheckError, "startup failure"):
            release.release_checks()
        self.assertEqual(len(fixtures), 2)
        for fixture in fixtures:
            fixture.close.assert_called_once()

    def test_cleanup_failure_does_not_mask_test_failure_or_skip_other_cleanup(self):
        fixtures, _, _, factory, command = self.make_runner(failure=release.ReleaseCheckError("test failed"), bad_cleanup=True)
        with patch.object(release, "Fixture", side_effect=factory), patch.object(release, "run_command", side_effect=command), redirect_stdout(io.StringIO()), self.assertRaisesRegex(release.ReleaseCheckError, "test failed") as caught:
            release.release_checks()
        self.assertIn("archive mismatch", str(caught.exception))
        for fixture in fixtures:
            fixture.close.assert_called_once()

    def test_swift_runs_both_packages_and_unsigned_build_with_owned_output(self):
        _, _, commands, factory, command = self.make_runner()
        with patch.object(release.sys, "platform", "darwin"), patch.object(release.shutil, "which", return_value="tool"), patch.object(release, "Fixture", side_effect=factory), patch.object(release, "run_command", side_effect=command), redirect_stdout(io.StringIO()):
            release.release_checks(swift=True)
        swift_commands = [args for args, _ in commands if args[0] == "swift"]
        self.assertEqual([args[3] for args in swift_commands], ["macos/SpektrKomandy", "macos"])
        self.assertTrue(all("--scratch-path" in args for args in swift_commands))
        self.assertNotIn("--skip", swift_commands[0])
        native = swift_commands[1]
        self.assertEqual(native[native.index("--skip") + 1],
                         "DvoynoyObzorSessionTests.SessionTests/testPhysicalTinySAUltraSmokeIfConnected")
        self.assertNotIn("--filter", native)
        xcode = commands[-1][0]
        self.assertEqual(xcode[0], "xcodebuild")
        self.assertIn("CODE_SIGNING_ALLOWED=NO", xcode)
        self.assertIn("-derivedDataPath", xcode)
        self.assertNotIn("test", xcode)


if __name__ == "__main__":
    unittest.main()

class IsolatedTkProcessTests(unittest.TestCase):
    def test_child_runs_exact_named_case_and_reports_success(self):
        case = release.IsolatedTkTest('tests.test_release_v040.AbsolutePowerTests.test_dbw_and_dbm_report_the_same_physical_power')
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(result.testsRun,1)
        self.assertTrue(result.wasSuccessful(),result.errors+result.failures)
        self.assertEqual(result.skipped,[])

    def test_unknown_case_cannot_be_reported_as_success(self):
        case = release.IsolatedTkTest('tests.test_release_v040.NoSuchCase')
        result = unittest.TestResult()
        case.run(result)
        self.assertFalse(result.wasSuccessful())

    def test_gui_cases_are_partitioned_without_dropping_any_ids(self):
        suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_windows_sweep_average')
        def flatten(items):
            for item in items:
                if isinstance(item,unittest.TestSuite):yield from flatten(item)
                else:yield item
        before=list(flatten(suite))
        after=list(flatten(release.isolate_tk_cases(suite)))
        self.assertEqual([test.id() for test in before],[test.id() for test in after])
        expected=[test for test in before if type(test).__name__=='AverageActualTkHTTPTests']
        self.assertEqual(sum(isinstance(test,release.IsolatedTkTest) for test in after),len(expected))
        self.assertGreater(len(expected),0)
