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
        with redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            release.run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                                name="timeout", environment=self.environment, directory=self.directory,
                                timeout=0.15, shutdown_timeout=2)

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
        with redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            release.run_command([sys.executable, "-c", parent], name="outer-timeout",
                                environment=self.environment, directory=self.directory,
                                timeout=0.8, shutdown_timeout=2)
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
        xcode = commands[-1][0]
        self.assertEqual(xcode[0], "xcodebuild")
        self.assertIn("CODE_SIGNING_ALLOWED=NO", xcode)
        self.assertIn("-derivedDataPath", xcode)
        self.assertNotIn("test", xcode)


if __name__ == "__main__":
    unittest.main()
