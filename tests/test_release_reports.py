"""Release evidence survives success, failure and cleanup, without archives."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests import run_release_checks as release
from tests import test_release_checks as fixtures


class ReleaseReportTests(unittest.TestCase):
    make_runner = fixtures.OrchestrationTests.make_runner

    def run_owned(self, destination, *, failure=None, bad_cleanup=False):
        children, _, _, factory, command = self.make_runner(failure=failure, bad_cleanup=bad_cleanup)

        def log_command(args, **kwargs):
            directory = kwargs['directory']
            (directory / 'synthetic.log').write_text('owned diagnostics', encoding='utf-8')
            (directory / 'do-not-copy.sqlite').write_bytes(b'private temporary archive')
            return command(args, **kwargs)

        with patch.object(release, 'Fixture', side_effect=factory), patch.object(release, 'run_command', side_effect=log_command), redirect_stdout(io.StringIO()):
            release.release_checks(report_dir=destination)
        return children

    def test_success_preserves_logs_and_results_but_not_archives(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'reports'
            children = self.run_owned(destination)
            runs = list(destination.iterdir())
            self.assertEqual(len(runs), 1)
            summary = json.loads((runs[0] / 'summary.json').read_text())
            self.assertTrue(summary['successful'])
            self.assertEqual(summary['hardware_validation'], 'not_run')
            self.assertTrue((runs[0] / 'python-results.json').is_file())
            self.assertEqual((runs[0] / 'synthetic.log').read_text(), 'owned diagnostics')
            self.assertFalse((runs[0] / 'do-not-copy.sqlite').exists())
            for child in children:
                child.close.assert_called_once()
            self.run_owned(destination)
            self.assertEqual(len(list(destination.iterdir())), 2)

    def test_test_failure_cleanup_failure_and_interrupt_leave_evidence(self):
        cases = [(release.ReleaseCheckError('test failed'), False),
                 (release.ReleaseCheckError('test failed'), True), (KeyboardInterrupt(), False)]
        for failure, bad_cleanup in cases:
            with self.subTest(failure=type(failure).__name__, bad_cleanup=bad_cleanup), tempfile.TemporaryDirectory() as root:
                expected = release.ReleaseCheckError if bad_cleanup else type(failure)
                with self.assertRaises(expected):
                    self.run_owned(Path(root), failure=failure, bad_cleanup=bad_cleanup)
                summary = json.loads(next(Path(root).glob('run-*/summary.json')).read_text())
                self.assertFalse(summary['successful'])
                self.assertIn(type(failure).__name__, summary['error'])
                self.assertEqual(bool(summary['cleanup_errors']), bad_cleanup)

    def test_preflight_failure_also_writes_summary(self):
        with tempfile.TemporaryDirectory() as root, patch.object(release.sys, 'platform', 'linux'), redirect_stdout(io.StringIO()):
            with self.assertRaises(release.ReleaseCheckError):
                release.release_checks(swift=True, report_dir=Path(root))
            summary = json.loads(next(Path(root).glob('run-*/summary.json')).read_text())
            self.assertFalse(summary['successful'])
            self.assertIn('requires macOS', summary['error'])

    def test_public_cli_forwards_report_directory(self):
        with patch.object(release, 'release_checks') as run:
            self.assertEqual(release.main(['--report-dir', 'owned-reports']), 0)
        self.assertEqual(run.call_args.kwargs['report_dir'], Path('owned-reports'))
