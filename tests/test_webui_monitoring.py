import shutil
import subprocess
import unittest
from pathlib import Path


class MonitoringWebTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for executable WebUI tests')
    def test_project_scope_and_explicit_guard_actions(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which('node'),str(root/'tests/webui_monitoring.cjs'),str(root/'radioinspector/web/static/monitoring.js'),str(root/'radioinspector/web/static/monitoring-research.js'),str(root/'radioinspector/web/static/monitoring-operations.js')],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
