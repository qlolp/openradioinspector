"""Modern checks use the existing owned subprocess and diagnostic lifecycle."""
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import run_release_checks as release
from tests.swift_version_fixtures import SUPPORTED_SWIFT_OUTPUTS, UNSUPPORTED_SWIFT_OUTPUTS


class ModernTemporaryRootTests(unittest.TestCase):
    def test_deep_preferred_temp_allows_real_nested_admin_socket_and_cleans_only_owned_root(self):
        with tempfile.TemporaryDirectory() as folder:
            preferred = Path(folder) / ("deep-" + "x" * 80)
            preferred.mkdir()
            stale = preferred / "preexisting-marker"
            stale.write_text("keep")
            captured = []
            previous_tempdir = tempfile.tempdir

            def check(directory, environment, **kwargs):
                captured.append(directory)
                self.assertEqual(environment["TMPDIR"], str(directory / "children"))
                self.assertEqual(environment["TEMP"], environment["TMPDIR"])
                self.assertEqual(environment["TMP"], environment["TMPDIR"])
                if os.name == "posix":
                    address = directory / "children/.tmpABC/candidate/.ori-runtime/core-admin.sock"
                    address.parent.mkdir(parents=True)
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                        listener.bind(str(address))
                    self.assertLessEqual(len(str(address).encode("utf-8")), 103)
                    self.assertEqual(directory.parent, Path("/tmp").resolve())
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                else:
                    self.assertEqual(directory.parent, preferred.resolve())

            try:
                with patch.object(release.tempfile, "gettempdir", return_value=str(preferred)), \
                        patch.object(release, "run_modern_checks", side_effect=check), \
                        redirect_stdout(io.StringIO()):
                    release.release_checks(modern=True, research_python=sys.executable)
            finally:
                self.assertEqual(stale.read_text(), "keep")
                for directory in captured:
                    self.assertFalse(directory.exists())
            self.assertEqual(len(captured), 1)
            self.assertEqual(tempfile.tempdir, previous_tempdir)

    def test_short_preferred_parent_is_honored_and_owned_directory_is_private(self):
        parent = Path("/tmp").resolve() if os.name == "posix" else Path(tempfile.gettempdir()).resolve()
        with tempfile.TemporaryDirectory(prefix="", dir=parent) as folder:
            preferred = Path(folder).resolve()
            if os.name == "posix" and len(str(preferred).encode("utf-8")) > 16:
                preferred = parent
            with patch.object(release.tempfile, "gettempdir", return_value=str(preferred)):
                with release._release_temporary_directory(modern=True) as temporary:
                    directory = Path(temporary)
                    self.assertEqual(directory.parent, preferred)
                    self.assertTrue(directory.name.startswith("ori-release-checks-"))
                    if os.name == "posix":
                        self.assertLessEqual(len(str(preferred).encode("utf-8")), 16)
                        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                    (directory / "owned-marker").write_text("owned")
                self.assertFalse(directory.exists())

    def test_modern_parent_limit_uses_canonical_utf8_bytes(self):
        fallback = Path("/tmp")
        for preferred in (Path("/" + "x" * 15), Path("/" + "x" * 16), Path("/" + "é" * 8)):
            expected = preferred if len(str(preferred).encode("utf-8")) <= 16 else fallback
            with self.subTest(preferred=str(preferred)), \
                    patch.object(release, "os", SimpleNamespace(name="posix")), \
                    patch.object(release.tempfile, "gettempdir", return_value="short-alias"), \
                    patch.object(release.Path, "resolve", side_effect=[preferred, fallback]) as resolve, \
                    patch.object(release.Path, "is_dir", return_value=True), \
                    patch.object(release.tempfile, "TemporaryDirectory") as create:
                self.assertIs(release._release_temporary_directory(modern=True), create.return_value)
                create.assert_called_once_with(prefix="ori-release-checks-", dir=expected)
                self.assertEqual(resolve.call_count, 1 if expected == preferred else 2)

    def test_legacy_and_windows_keep_the_default_constructor(self):
        for modern, platform in ((False, "posix"), (False, "nt"), (True, "nt")):
            with self.subTest(modern=modern, platform=platform), \
                    patch.object(release, "os", SimpleNamespace(name=platform)), \
                    patch.object(release.tempfile, "gettempdir") as default, \
                    patch.object(release.Path, "resolve") as resolve, \
                    patch.object(release.tempfile, "TemporaryDirectory") as create:
                self.assertIs(release._release_temporary_directory(modern=modern), create.return_value)
                create.assert_called_once_with(prefix="ori-release-checks-")
                default.assert_not_called()
                resolve.assert_not_called()

    def test_legacy_long_preferred_parent_is_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            preferred = Path(folder) / ("deep-" + "x" * 80)
            preferred.mkdir()
            with patch.object(release.tempfile, "gettempdir", return_value=str(preferred)):
                with release._release_temporary_directory(modern=False) as temporary:
                    directory = Path(temporary)
                    self.assertEqual(directory.parent.resolve(), preferred.resolve())
                self.assertFalse(directory.exists())

    def test_no_safe_parent_fails_before_creating_a_scope(self):
        for preferred in (Path("/" + "x" * 20), OSError("unavailable preferred directory")):
            with self.subTest(preferred=str(preferred)), \
                    patch.object(release, "os", SimpleNamespace(name="posix")), \
                    patch.object(release.tempfile, "gettempdir", return_value="preferred"), \
                    patch.object(release.Path, "resolve", side_effect=[preferred, Path("/" + "y" * 20)]), \
                    patch.object(release.tempfile, "TemporaryDirectory") as create:
                with self.assertRaisesRegex(release.ReleaseCheckError, "at most 16 UTF-8 bytes"):
                    release._release_temporary_directory(modern=True)
                create.assert_not_called()


class ModernizationChecksTests(unittest.TestCase):
    def swift_version_result(self, output, *, exit_code=0):
        from tools.modernization import checks
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with patch.object(checks, 'tool', side_effect=lambda name, env: '/prepared/' + name), \
                    patch.object(checks, 'npm_command', return_value=['/prepared/node', '/prepared/npm-cli.js']):
                plan = checks.commands(release.ROOT, directory, {}, research_python=sys.executable, swift=True)
            version = next(check for check in plan if check.name == 'modern-swift-version')
            version = replace(version, command=[sys.executable, '-c',
                f'import sys; print({output!r}, end=""); sys.exit({exit_code})'])
            tap = '# tests 1\n# pass 1\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0'
            following = checks.Check('modern-following-suite', [sys.executable, '-c', f'print({tap!r})'], 'node')
            failure = None
            with patch.object(release, 'modern_commands', return_value=[version, following]), redirect_stdout(io.StringIO()):
                try:
                    release.release_checks(modern=True, research_python=sys.executable, report_dir=directory / 'reports')
                except release.ReleaseCheckError as error:
                    failure = error
            retained = next((directory / 'reports').glob('run-*'))
            report = json.loads((retained / 'modern-results.json').read_text())
            log = (retained / 'modern-swift-version.log').read_text()
            return failure, report, log

    def test_swift_version_gate_accepts_supported_compiler_output_formats(self):
        for name, output in SUPPORTED_SWIFT_OUTPUTS:
            with self.subTest(format=name):
                failure, report, log = self.swift_version_result(output)
                self.assertIsNone(failure, str(failure))
                self.assertTrue(report['successful'])
                self.assertEqual([check['status'] for check in report['checks']], ['passed', 'passed'])
                self.assertIsNone(report['checks'][0]['tests_run'])
                self.assertEqual(report['checks'][1]['tests_run'], 1)
                self.assertIn(output.rstrip(), log)

    def test_swift_version_gate_rejects_wrong_ambiguous_and_unrelated_versions(self):
        for name, output in UNSUPPORTED_SWIFT_OUTPUTS:
            with self.subTest(format=name):
                failure, report, _log = self.swift_version_result(output)
                self.assertIsInstance(failure, release.ReleaseCheckError)
                self.assertFalse(report['successful'])
                self.assertEqual([check['status'] for check in report['checks']], ['failed', 'not_run'])

    def test_swift_version_gate_still_requires_a_successful_owned_command(self):
        failure, report, _log = self.swift_version_result('Swift version 6.4.2\n', exit_code=9)
        self.assertIsInstance(failure, release.ReleaseCheckError)
        self.assertFalse(report['successful'])
        self.assertEqual([check['status'] for check in report['checks']], ['failed', 'not_run'])

    def test_missing_packaged_ui_fails_before_any_compiler_command(self):
        from tools.modernization import checks
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with patch.object(checks, 'tool', side_effect=lambda name, env: '/prepared/' + name), \
                    patch.object(checks, 'npm_command', return_value=['/prepared/node', '/prepared/npm-cli.js']):
                plan = checks.commands(release.ROOT, directory, {}, research_python='research-python', swift=True)
            guards = [item for item in plan if item.name == 'modern-ui-inputs']
            self.assertEqual(len(guards), 1, 'Missing packaged UI must fail before compilation')
            guard = guards[0]
            compiler_positions = [index for index, item in enumerate(plan) if item.command[0] == '/prepared/cargo']
            self.assertTrue(compiler_positions)
            self.assertLess(plan.index(guard), min(compiler_positions))
            marker = directory / 'compiler-ran'
            blocked_plan = [
                checks.Check(guard.name, guard.command + ['--destination', str(directory / 'missing-ui')]),
                checks.Check('modern-compiler-marker', [sys.executable, '-c',
                    f'from pathlib import Path; Path({str(marker)!r}).write_text("unexpected compiler admission")'], 'node'),
            ]
            with patch.object(release, 'modern_commands', return_value=blocked_plan), redirect_stdout(io.StringIO()):
                with self.assertRaises(release.ReleaseCheckError):
                    release.release_checks(modern=True, research_python=sys.executable, report_dir=directory / 'reports')
            self.assertFalse(marker.exists())
            report = json.loads(next((directory / 'reports').glob('run-*/modern-results.json')).read_text())
            self.assertEqual([item['status'] for item in report['checks']], ['failed', 'not_run'])

    def test_public_profile_and_explicit_interpreter_reach_the_existing_runner(self):
        with patch.object(release, 'release_checks') as run:
            self.assertEqual(release.main(['--modern', '--research-python', 'research-python', '--swift']), 0)
        self.assertTrue(run.call_args.kwargs['modern'])
        self.assertEqual(run.call_args.kwargs['research_python'], 'research-python')
        self.assertTrue(run.call_args.kwargs['swift'])

    def test_missing_interpreter_fails_and_keeps_modern_profile_evidence(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(release.ReleaseCheckError, 'research-python'):
                release.release_checks(modern=True, report_dir=Path(folder))
            report = json.loads(next(Path(folder).glob('run-*/summary.json')).read_text())
            self.assertFalse(report['successful'])
            self.assertEqual(report['profile'], 'modern')
            self.assertEqual(report['hardware_validation'], 'not_run')
            self.assertTrue(next(Path(folder).glob('run-*/modern-results.json')).is_file())

    def test_real_child_success_counts_tests_and_failure_retains_diagnostics(self):
        from tools.modernization.checks import Check
        cases = [
            (0, '1..2\n# tests 2\n# pass 2\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n'),
            (7, 'owned modern failure\n'),
            (0, '1..0\n# tests 0\n# pass 0\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n'),
        ]
        for status, output in cases:
            with self.subTest(status=status, output=output), tempfile.TemporaryDirectory() as folder:
                command = [sys.executable, '-c', f'import sys; print({output!r}); sys.exit({status})']
                checks = [Check('modern-tiny-child', command, 'node')]
                with patch.object(release, 'modern_commands', return_value=checks), redirect_stdout(io.StringIO()):
                    if status == 0 and '# tests 2' in output:
                        release.release_checks(modern=True, research_python=sys.executable, report_dir=Path(folder))
                    else:
                        with self.assertRaises(release.ReleaseCheckError):
                            release.release_checks(modern=True, research_python=sys.executable, report_dir=Path(folder))
                destination = next(Path(folder).glob('run-*'))
                report = json.loads((destination / 'modern-results.json').read_text())
                self.assertEqual(report['successful'], status == 0 and '# tests 2' in output)
                self.assertIn(output.strip(), (destination / 'modern-tiny-child.log').read_text())
                self.assertEqual(report['checks'][0]['status'], 'passed' if report['successful'] else 'failed')
                if report['successful']:
                    self.assertEqual(report['checks'][0]['tests_run'], 2)
                self.assertFalse(any(destination.glob('*.sqlite')))

    def test_timeout_uses_owned_cleanup_and_preserves_partial_modern_output(self):
        from tools.modernization.checks import Check
        command = [sys.executable, '-u', '-c', 'import time; print("modern child started", flush=True); time.sleep(60)']
        with tempfile.TemporaryDirectory() as folder, patch.object(release, 'modern_commands', return_value=[Check('modern-timeout',command,'node')]), redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.TimeoutExpired):
                release.release_checks(modern=True, research_python=sys.executable, command_timeout=2, shutdown_timeout=3, report_dir=Path(folder))
            destination = next(Path(folder).glob('run-*'))
            self.assertIn('modern child started', (destination / 'modern-timeout.log').read_text())
            self.assertFalse(json.loads((destination / 'summary.json').read_text())['successful'])
            self.assertFalse(json.loads((destination / 'modern-results.json').read_text())['successful'])

    def test_framework_counts_reject_ignored_filtered_empty_and_skipped_suites(self):
        from tools.modernization.checks import count_tests
        self.assertEqual(count_tests('rust', 'test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n'), 3)
        for kind, text in [
            ('rust','test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out'),
            ('rust','test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out'),
            ('rust','test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 1 filtered out'),
            ('node','# tests 3\n# pass 2\n# fail 0\n# skipped 1\n# cancelled 0\n# todo 0'),
        ]:
            with self.subTest(kind=kind,text=text), self.assertRaises(ValueError):
                count_tests(kind,text)

    def test_missing_tool_and_empty_modern_plan_never_pass(self):
        for plan in (None, []):
            with self.subTest(plan=plan), tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
                context = patch.dict('os.environ', {'PATH':''}) if plan is None else patch.object(release,'modern_commands',return_value=plan)
                with context, self.assertRaises(release.ReleaseCheckError):
                    release.release_checks(modern=True,research_python=sys.executable,report_dir=Path(folder))
                report=json.loads(next(Path(folder).glob('run-*/modern-results.json')).read_text())
                self.assertFalse(report['successful'])
                self.assertIn('missing' if plan is None else 'no test suites',report['error'])

    def test_structured_reports_cannot_hide_errors_or_missing_fields(self):
        from tools.modernization.checks import count_tests
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'result.json'
            good={'successful':True,'discovered_tests':2,'tests_run':2,'skipped':[],'failures':0,'errors':0}
            path.write_text(json.dumps(good))
            self.assertEqual(count_tests('python','',path),2)
            for bad in [dict(good,errors=1),dict(good,failures=1),dict(good,discovered_tests=3),dict(good,skipped=[['case','reason']]),dict(good,tests_run=1,discovered_tests=True)]:
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    count_tests('python','',path)
            web={'success':True,'numTotalTests':2,'numPassedTests':2,'numFailedTests':0,'numPendingTests':0,'numTodoTests':0}
            path.write_text(json.dumps(web))
            self.assertEqual(count_tests('vitest','',path),2)
            del web['numPendingTests']
            path.write_text(json.dumps(web))
            with self.assertRaises(ValueError):
                count_tests('vitest','',path)

    def test_python_helper_reports_discovery_and_rejects_skips_or_empty_suites(self):
        helper = release.ROOT / 'tools/modernization/python_suite.py'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for source, expected in [('import unittest\nclass Good(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+2,4)\n',0),
                                     ('import unittest\nclass Bad(unittest.TestCase):\n @unittest.skip("not allowed")\n def test_skip(self): pass\n',1),('',1)]:
                (root / 'test_case.py').write_text(source)
                report = root / 'results.json'
                completed = subprocess.run([sys.executable,str(helper),'--start-directory',str(root),'--report',str(report)],capture_output=True,text=True,timeout=15)
                self.assertEqual(completed.returncode,expected,completed.stderr)
                result=json.loads(report.read_text())
                self.assertEqual(result['successful'],expected==0)
                self.assertEqual(result['tests_run'],result['discovered_tests'])


if __name__ == '__main__':
    unittest.main()
