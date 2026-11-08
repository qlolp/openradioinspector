"""Execute real measurement-result handlers with synthetic data, never hardware or user archives."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebMeasurementResultsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_measurement_results_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_measurement_results.cjs"),
                   str(static / "measurement-results.js"), str(static / "platform.js")]
        if base := os.environ.get("ORI_MEAS_SMOKE_BASE_URL"):
            command.append(base)
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("measurement-results behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/measurement-results.js"'), html.index('src="/static/platform.js"'))
        for name in ("run", "clear", "save", "methodology", "code", "frequency",
                     "started", "ended", "values", "source", "status", "summary", "items"):
            self.assertIn(f'id="meas-{name}"', html)
        for name in ("spectrum-canvas", "waterfall-canvas", "comparison-canvas", "three-thresholds-canvas",
                     "threshold-canvas", "mask-canvas", "average-overlay", "average-delta",
                     "context-canvas", "raster-items"):
            self.assertIn(f'id="{name}"', html)
        for text in ("Результаты измерений с явной методикой", "числовые значения отдельно",
                     "Методика определяется явно", "исходные измерения архива не изменяются"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
