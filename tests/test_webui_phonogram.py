"""Execute real phonogram handlers with synthetic data, never hardware or user archives."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebPhonogramTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_phonogram_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_phonogram.cjs"),
                   str(static / "phonogram.js"), str(static / "platform.js")]
        if base := os.environ.get("ORI_PHONO_SMOKE_BASE_URL"):
            command.append(base)
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("phonogram behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/phonogram.js"'), html.index('src="/static/platform.js"'))
        for name in ("start", "stop", "refresh", "stream", "source", "status", "summary", "items"):
            self.assertIn(f'id="phono-{name}"', html)
        for name in ("spectrum-canvas", "waterfall-canvas", "comparison-canvas", "three-thresholds-canvas",
                     "threshold-canvas", "mask-canvas", "average-overlay", "average-delta",
                     "context-canvas", "raster-items", "meas-items", "counter-items", "dvb-items"):
            self.assertIn(f'id="{name}"', html)
        for text in ("пятисекундной предысторией", "Предыстория фиксирована",
                     "панель их не генерирует", "ничего не пишется"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
