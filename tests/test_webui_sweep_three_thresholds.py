"""Execute three-line WebUI handlers against synthetic data, never hardware."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebSweepThreeThresholdsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_three_threshold_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_sweep_three_thresholds.cjs"),
                   str(static / "sweep-three-thresholds.js"), str(static / "platform.js")]
        if base := os.environ.get("ORI_THREE_THRESHOLDS_SMOKE_BASE_URL"):
            command.append(base)
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("three-threshold behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/sweep-three-thresholds.js"'), html.index('src="/static/platform.js"'))
        for name in ("level-1", "level-2", "level-3", "run", "clear", "source", "status", "summary", "canvas",
                     "line-1-summary", "line-2-summary", "line-3-summary", "line-1-regions", "line-2-regions", "line-3-regions"):
            self.assertIn(f'id="three-thresholds-{name}"', html)
        for name in ("spectrum-canvas", "waterfall-canvas", "comparison-canvas", "comparison-catalog",
                     "threshold-canvas", "mask-canvas", "average-overlay", "average-delta"):
            self.assertIn(f'id="{name}"', html)
        for text in ('value="-90"', 'value="-80"', 'value="-70"', "не нормативные пределы", "в любом порядке", "вложенными кольцами"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
