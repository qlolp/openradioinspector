from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest


class ReleaseAnnotationTests(unittest.TestCase):
    def test_failed_report_exposes_the_actual_assertion_without_changing_result(self):
        from tools.release.annotate_checks import report
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'summary.json').write_text(json.dumps({'successful': False, 'error': 'child exited 1'}))
            (root / 'suite.log').write_text('FAIL: test_owned_cleanup\nTraceback:\nAssertionError: child still active 50%\n')
            output = io.StringIO()
            with redirect_stdout(output):
                count = report(root)
            self.assertEqual(count, 2)
            self.assertIn('child exited 1', output.getvalue())
            self.assertIn('AssertionError: child still active 50%25', output.getvalue())
            self.assertIn('%0A', output.getvalue())

    def test_successful_report_does_not_emit_error_annotations(self):
        from tools.release.annotate_checks import report
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'summary.json').write_text(json.dumps({'successful': True, 'error': None}))
            (root / 'suite.log').write_text('FAIL: old text in a diagnostic fixture\n')
            output = io.StringIO()
            with redirect_stdout(output): self.assertEqual(report(root), 0)
            self.assertEqual(output.getvalue(), '')
