"""Execute real frequency raster handlers with synthetic data, never hardware or user archives."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebFrequencyRasterTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_raster_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_frequency_raster.cjs"),
                   str(static / "frequency-raster.js"), str(static / "platform.js")]
        if base := os.environ.get("ORI_RASTER_SMOKE_BASE_URL"):
            command.append(base)
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("frequency-raster behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/frequency-raster.js"'), html.index('src="/static/platform.js"'))
        for name in ("run", "clear", "editor", "file", "template", "save", "draft", "source",
                     "status", "summary", "items", "candidates"):
            self.assertIn(f'id="raster-{name}"', html)
        for name in ("spectrum-canvas", "waterfall-canvas", "comparison-canvas", "three-thresholds-canvas",
                     "threshold-canvas", "mask-canvas", "average-overlay", "average-delta", "context-canvas"):
            self.assertIn(f'id="{name}"', html)
        for text in ("Округление частоты обнаруженных сигналов", "Исходные частоты архива не изменяются",
                     "Частоты вне всех записей не округляются", "start_hz + k·grid_step_hz"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
