"""Behavioral checks execute actual production functions with small DOM doubles."""
import shutil
import subprocess
import unittest
from pathlib import Path


class WebReleaseContextTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node required for WebUI behavior')
    def test_context_events_and_frequency_grid(self):
        for name in ['context','events','large','grid']:
            with self.subTest(name=name):
                result=subprocess.run([shutil.which('node'),str(Path(__file__).with_name('webui_release_context.cjs')),name],capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
