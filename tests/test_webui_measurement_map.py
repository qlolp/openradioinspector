"""Execute real measurement-map handlers with synthetic data, never user archives."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebMeasurementMapTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_map_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_measurement_map.cjs"),
                   str(static / "measurement-map.js"), str(static / "platform.js")]
        if base := os.environ.get("ORI_MAP_SMOKE_BASE_URL"):
            command.append(base)
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("measurement-map behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/measurement-map.js"'), html.index('src="/static/platform.js"'))
        for name in ("run", "clear", "editor", "source", "status", "summary", "items", "canvas"):
            self.assertIn(f'id="map-{name}"', html)
        for name in ("spectrum-canvas", "waterfall-canvas", "comparison-canvas", "three-thresholds-canvas",
                     "threshold-canvas", "mask-canvas", "average-overlay", "average-delta",
                     "context-canvas", "raster-items", "meas-items", "counter-items", "dvb-items",
                     "phono-items", "siglist-items"):
            self.assertIn(f'id="{name}"', html)
        for text in ("Карта точек измерений и секторов", "дуге большого круга",
                     "не измерение и не привязка GPS", "не нормативная карта"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
