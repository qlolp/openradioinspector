"""Execute real candidate-rounding handlers with synthetic data, no private archive."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebFrequencyRoundingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_frequency_rounding_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_frequency_rounding.cjs"),
                   str(static / "frequency-rounding.js"), str(static / "platform.js")]
        if base := os.environ.get("ORI_ROUNDING_SMOKE_BASE_URL"):
            command.append(base)
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("frequency-rounding behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="/static/frequency-rounding.js"'), html.index('src="/static/platform.js"'))
        for name in ("candidate", "run", "clear", "editor", "file", "template", "save", "draft", "source", "status", "summary"):
            self.assertIn(f'id="rounding-{name}"', html)
        for name in ("spectrum-canvas", "waterfall-canvas", "comparison-canvas", "three-thresholds-canvas",
                     "threshold-canvas", "mask-canvas", "average-overlay", "average-delta", "context-canvas"):
            self.assertIn(f'id="{name}"', html)
        self.assertIn("Кандидат из всех потоков текущего сеанса", html)
        self.assertIn("Сохранить проверенный профиль", html)


if __name__ == "__main__":
    unittest.main()
