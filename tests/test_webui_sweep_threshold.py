"""Execute threshold UI handlers without hardware or private archive data."""

import shutil
import subprocess
import unittest
from pathlib import Path


class WebSweepThresholdTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_threshold_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests/webui_sweep_threshold.cjs"),
             str(static / "sweep-threshold.js"), str(static / "platform.js")],
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("threshold behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/sweep-threshold.js"'), html.index('src="/static/platform.js"'))
        for name in ("threshold-canvas", "threshold-window", "threshold-margin", "threshold-run", "threshold-regions",
                     "spectrum-canvas", "waterfall-canvas", "comparison-canvas", "comparison-catalog"):
            self.assertIn(f'id="{name}"', html)
        self.assertIn('value="21"', html)
        self.assertIn('value="6"', html)
        self.assertIn("не калиброванный шум", html)


if __name__ == "__main__":
    unittest.main()
