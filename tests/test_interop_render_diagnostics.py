import shutil
import subprocess
import unittest
from pathlib import Path


class RenderDiagnosticTests(unittest.TestCase):
    def test_actual_diagnostic_installer_preserves_native_frame_and_cancel_semantics(self):
        root = Path(__file__).resolve().parents[1]
        command = [shutil.which('node') or 'node', str(root / 'tests/interop_render_diagnostics.mjs'),
                   (root / 'tools/modernization/client_render_diagnostics.mjs').as_uri()]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
