"""Execute the serial session form without connecting any instrument."""
import shutil
import subprocess
import unittest
from pathlib import Path


class WebSerialSessionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for WebUI behavior tests')
    def test_serial_session_creation_is_explicit_and_does_not_start_devices(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([
            shutil.which('node'), str(root / 'tests/webui_serial_session.cjs'),
            str(root / 'radioinspector/web/static/serial-session.js'),
        ], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_form_is_accessible_from_platform(self):
        html = (Path(__file__).resolve().parents[1] / 'radioinspector/web/static/platform.html').read_text()
        self.assertIn('src="/static/serial-session.js"', html)
        for name in ('form', 'name', 'tinysa', 'receiver', 'submit', 'status'):
            self.assertIn(f'id="serial-session-{name}"', html)
