"""Execute real WebUI comparison handlers with controlled fetch/lifecycle races."""

import shutil
import subprocess
import unittest
from pathlib import Path


class WebSweepComparisonTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_comparison_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests/webui_sweep_comparison.cjs"),
             str(static / "sweep-comparison.js"), str(static / "platform.js")],
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("comparison behavior checks completed", result.stdout)

    def test_asset_and_panel_wired(self):
        root = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (root / "platform.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="/static/sweep-comparison.js"'), html.index('src="/static/platform.js"'))
        self.assertIn('id="comparison-canvas"', html)
        self.assertIn('id="comparison-clear"', html)
        self.assertIn('id="spectrum-canvas"', html)
        self.assertIn('id="waterfall-canvas"', html)


if __name__ == "__main__":
    unittest.main()
