"""Diagnostic timer ownership without reproducing an unbounded native hang."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

from tests import run_release_checks as release


class DiagnosticTimerOwnershipTests(unittest.TestCase):
    def test_darwin_315_returns_after_success_without_entering_blocked_native_cancel(self):
        published = threading.Event()
        returned = threading.Event()
        cancel_entered = threading.Event()
        allow_cancel = threading.Event()
        results, errors = [], []

        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "result.json"

            def suite(path, **kwargs):
                path.write_text(json.dumps({"successful": True, "tests_run": 1}))
                published.set()
                return 0

            def blocked_native_cancel():
                cancel_entered.set()
                if not allow_cancel.wait(3):
                    raise RuntimeError("controlled cancellation gate was not released")

            def invoke():
                try:
                    results.append(release._run_diagnostic_python_suite(report, 60))
                except BaseException as error:
                    errors.append(error)
                finally:
                    returned.set()

            with patch.object(release.sys, "platform", "darwin"), \
                    patch.object(release.sys, "version_info", (3, 15, 2)), \
                    patch.object(release, "run_python_suite", side_effect=suite) as run, \
                    patch.object(release.faulthandler, "enable"), \
                    patch.object(release.faulthandler, "dump_traceback_later") as arm, \
                    patch.object(release.faulthandler, "cancel_dump_traceback_later", side_effect=blocked_native_cancel) as cancel:
                invocation = threading.Thread(target=invoke, name="controlled-diagnostic-call")
                invocation.start()
                try:
                    self.assertTrue(published.wait(2), "synthetic suite did not publish")
                    self.assertTrue(json.loads(report.read_text())["successful"])
                    self.assertTrue(returned.wait(1), "successful suite is stuck at native cancellation")
                    self.assertFalse(cancel_entered.is_set())
                finally:
                    allow_cancel.set()
                    invocation.join(3)
                self.assertFalse(invocation.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [0])
                run.assert_called_once_with(report, diagnostics=True)
                arm.assert_not_called()
                cancel.assert_not_called()

    def test_platform_policy_preserves_single_selection_and_cleanup_for_every_outcome(self):
        profiles = (
            ("darwin", (3, 15, 0), False), ("darwin", (3, 15, 1), False),
            ("darwin", (3, 15, 2), False), ("darwin", (3, 15, 99), False),
            ("linux", (3, 15, 2), True), ("win32", (3, 15, 2), True),
            ("darwin", (3, 14, 1), True), ("darwin", (3, 16, 0), True),
            ("darwin", (3, 12, 10), False),
        )
        for platform, version, native in profiles:
            for outcome in (0, 1, KeyboardInterrupt(), RuntimeError("suite failed")):
                with self.subTest(platform=platform, version=version, outcome=type(outcome).__name__), \
                        patch.object(release.sys, "platform", platform), \
                        patch.object(release.sys, "version_info", version), \
                        patch.object(release, "run_python_suite") as suite, \
                        patch.object(release, "_GilSafeWatchdog") as watchdog, \
                        patch.object(release.faulthandler, "enable") as enable, \
                        patch.object(release.faulthandler, "dump_traceback_later") as arm, \
                        patch.object(release.faulthandler, "cancel_dump_traceback_later") as cancel:
                    path = Path("unused.json")
                    if isinstance(outcome, BaseException):
                        suite.side_effect = outcome
                        with self.assertRaises(type(outcome)) as caught:
                            release._run_diagnostic_python_suite(path, 60, only_test="synthetic.Case.test_one")
                        self.assertIs(caught.exception, outcome)
                    else:
                        suite.return_value = outcome
                        self.assertEqual(release._run_diagnostic_python_suite(path, 60, only_test="synthetic.Case.test_one"), outcome)
                    suite.assert_called_once_with(path, diagnostics=True, only_test="synthetic.Case.test_one")
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

    def test_successful_darwin_suite_cannot_hide_a_watchdog_close_failure(self):
        failure = release.ReleaseCheckError("owned watchdog did not join")
        with patch.object(release.sys, "platform", "darwin"), \
                patch.object(release.sys, "version_info", (3, 15, 2)), \
                patch.object(release, "run_python_suite", return_value=0), \
                patch.object(release, "_GilSafeWatchdog") as watchdog, \
                patch.object(release.faulthandler, "enable"), \
                patch.object(release.faulthandler, "dump_traceback_later") as arm, \
                patch.object(release.faulthandler, "cancel_dump_traceback_later") as cancel:
            watchdog.return_value.close.side_effect = failure
            with self.assertRaises(release.ReleaseCheckError) as caught:
                release._run_diagnostic_python_suite(Path("unused.json"), 60)
            self.assertIs(caught.exception, failure)
            arm.assert_not_called()
            cancel.assert_not_called()

    def test_close_waits_for_the_actual_dump_then_stops_without_another_dump(self):
        entered, finish, closing, closed = (threading.Event() for _ in range(4))
        errors = []
        owned_dumps = []
        watchdog = release._GilSafeWatchdog(0.01, io.StringIO())
        original_dump = release.faulthandler.dump_traceback

        def dump(**kwargs):
            if threading.current_thread() is not watchdog.thread:
                return original_dump(**kwargs)
            owned_dumps.append(kwargs)
            entered.set()
            if not finish.wait(3):
                raise RuntimeError("controlled dump was not released")

        def close():
            closing.set()
            try:
                watchdog.close()
            except BaseException as error:
                errors.append(error)
            finally:
                closed.set()

        with patch.object(release.faulthandler, "dump_traceback", side_effect=dump):
            watchdog.start()
            closer = threading.Thread(target=close, name="controlled-watchdog-close")
            try:
                self.assertTrue(entered.wait(2))
                closer.start()
                self.assertTrue(closing.wait(2))
                self.assertFalse(closed.wait(0.05), "close acknowledged before the dump returned")
            finally:
                finish.set()
                if closer.ident is not None:
                    closer.join(3)
                watchdog.close()
            self.assertFalse(closer.is_alive())
            self.assertFalse(watchdog.thread.is_alive())
            self.assertTrue(watchdog.stopped.is_set())
            self.assertEqual(errors, [])
            self.assertEqual(len(owned_dumps), 1)

    def test_real_child_repeats_full_gil_safe_dumps_joins_and_exits(self):
        script = textwrap.dedent('''
            import json
            from pathlib import Path
            import sys
            import threading
            from unittest.mock import patch
            from tests import run_release_checks as release

            native_stderr = sys.__stderr__
            repeated = threading.Event()
            created = []
            factory = release._GilSafeWatchdog

            class CountingStderr:
                count = 0
                def fileno(self):
                    return native_stderr.fileno()
                def write(self, value):
                    native_stderr.write(value)
                    if 'GIL-safe diagnostic watchdog' in value:
                        self.count += 1
                        if self.count >= 2:
                            repeated.set()
                def flush(self):
                    native_stderr.flush()

            stream = CountingStderr()
            def own(*args):
                value = factory(*args)
                created.append(value)
                return value

            def run_fixture(path, **kwargs):
                if not repeated.wait(3):
                    raise AssertionError('two real watchdog dumps did not complete')
                path.write_text(json.dumps({'successful': True, 'tests_run': 1}))
                return 0

            def reject_native(*args, **kwargs):
                raise AssertionError('Darwin315 entered the native timer path')

            with patch.object(release.sys, 'platform', 'darwin'), \\
                    patch.object(release.sys, 'version_info', (3, 15, 2)), \\
                    patch.object(release.sys, '__stderr__', stream), \\
                    patch.object(release, '_GilSafeWatchdog', side_effect=own), \\
                    patch.object(release, 'run_python_suite', side_effect=run_fixture), \\
                    patch.object(release.faulthandler, 'dump_traceback_later', side_effect=reject_native), \\
                    patch.object(release.faulthandler, 'cancel_dump_traceback_later', side_effect=reject_native):
                result = release._run_diagnostic_python_suite(Path(sys.argv[1]), 0.01)
            assert result == 0 and stream.count >= 2 and len(created) == 1
            assert created[0].stopped.is_set() and not created[0].thread.is_alive()
            assert not any(t.name == 'ori-diagnostic-watchdog' for t in threading.enumerate())
            print(json.dumps({'diagnostic_child_complete': True, 'dumps': stream.count, 'joined': True}), flush=True)
        ''')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "home").mkdir()
            environment = release.child_environment()
            environment.update(HOME=str(root / "home"), TMPDIR=str(root), TMP=str(root), TEMP=str(root))
            output = release.run_command([sys.executable, "-B", "-c", script, str(root / "result.json")],
                                         name="gil-safe-diagnostic-child", environment=environment,
                                         directory=root, timeout=10, shutdown_timeout=3)
            report = json.loads((root / "result.json").read_text())
            self.assertTrue(report["successful"])
            self.assertGreaterEqual(output.count("GIL-safe diagnostic watchdog"), 2)
            self.assertIn("run_fixture", output)
            self.assertIn("Current thread", output)
            marker = next(json.loads(line) for line in output.splitlines() if line.startswith('{"diagnostic_child_complete"'))
            self.assertTrue(marker["joined"])


if __name__ == "__main__":
    unittest.main()
