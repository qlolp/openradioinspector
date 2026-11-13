import shutil
import subprocess
import unittest
from pathlib import Path


class WebFieldStrengthTests(unittest.TestCase):
    def test_field_analysis_identity_scope_errors_and_unknown_uncertainty(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which('node') or 'node',
            str(root / 'tests/webui_field_strength.cjs'),
            str(root / 'radioinspector/web/static/field-strength.js'),
            str(root / 'tests/fixtures/field-strength-rms-v2.json')],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
