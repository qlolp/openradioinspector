"""Execute averaged-reference UI handlers with synthetic sweeps, never hardware."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebSweepAverageTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_average_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_sweep_average.cjs"),
                   str(static / "sweep-average.js"), str(static / "platform.js")]
        base = os.environ.get("ORI_AVERAGE_SMOKE_BASE_URL", "")
        detection_base = os.environ.get("ORI_AVERAGE_DETECTION_SMOKE_BASE_URL", "")
        if base or detection_base: command.extend([base, detection_base])
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("average behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/sweep-average.js"'), html.index('src="/static/platform.js"'))
        for name in ("average-count", "average-detect-threshold", "average-detect-bins",
                     "average-pin", "average-run", "average-clear", "average-reference",
                     "average-status", "average-summary", "average-overlay", "average-delta",
                     "spectrum-canvas", "waterfall-canvas", "comparison-canvas", "comparison-catalog",
                     "threshold-canvas", "mask-canvas"):
            self.assertIn(f'id="{name}"', html)
        for text in ("равными весами", "N+1", "не постоянный именованный", "без автоматического уменьшения N"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
