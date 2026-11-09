"""Synthetic executable coverage for the archived quality report WebUI."""
import shutil
import subprocess
import unittest
from pathlib import Path


class WebSweepQualityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_quality_behavior(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which('node'), str(root/'tests/webui_sweep_quality.cjs'),
                                 str(root/'radioinspector/web/static/sweep-quality.js')],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('quality behavior checks completed', result.stdout)

    def test_quality_assets_and_lifecycle(self):
        static = Path(__file__).resolve().parents[1]/'radioinspector/web/static'
        html = (static/'platform.html').read_text()
        self.assertLess(html.index('src="/static/sweep-quality.js"'), html.index('src="/static/platform.js"'))
        for name in ('anchor', 'margin', 'high', 'run', 'clear', 'status', 'summary'):
            self.assertIn(f'id="quality-{name}"', html)
        self.assertIn('не диагностирует перегрузку или помехи', html)
        js = (static/'platform.js').read_text()
        self.assertIn('OpenRadioSweepQuality?.update', js)
        self.assertIn('OpenRadioSweepQuality?.setConnected(true)', js)
        self.assertEqual(js.count('OpenRadioSweepQuality?.setConnected(false)'), 2)
