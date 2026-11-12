"""Modern checks use the existing owned subprocess and diagnostic lifecycle."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tests import run_release_checks as release


class ModernizationChecksTests(unittest.TestCase):
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
