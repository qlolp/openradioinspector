"""Run synthetic production WebUI display and handler tests without browser or hardware."""
import shutil
import subprocess
import unittest
from pathlib import Path


class WebSpectrumViewTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for WebUI handler tests")
    def test_spectrum_view_behavior(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests/webui_spectrum_view.cjs")],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("spectrum-view behavior checks completed", result.stdout)
