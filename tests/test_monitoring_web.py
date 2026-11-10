"""Runtime regressions for project pagination and optional browser preferences."""
import shutil
import subprocess
import unittest
from pathlib import Path


class MonitoringWebTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for WebUI behavior tests')
    def test_project_workspace(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run([shutil.which('node'), str(root / 'tests/webui_monitoring.cjs')],
                       cwd=root, check=True, timeout=30)
