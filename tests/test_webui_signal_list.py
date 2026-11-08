"""Execute real signal-list handlers with synthetic data, never hardware or user archives."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebSignalListTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_signal_list_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_signal_list.cjs"),
                   str(static / "signal-list.js"), str(static / "platform.js")]
        if base := os.environ.get("ORI_SIGLIST_SMOKE_BASE_URL"):
            command.append(base)
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("signal-list behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/signal-list.js"'), html.index('src="/static/platform.js"'))
        for name in ("export", "import", "stream", "name", "editor", "source", "status", "summary", "items"):
            self.assertIn(f'id="siglist-{name}"', html)
        for name in ("spectrum-canvas", "waterfall-canvas", "comparison-canvas", "three-thresholds-canvas",
                     "threshold-canvas", "mask-canvas", "average-overlay", "average-delta",
                     "context-canvas", "raster-items", "meas-items", "counter-items", "dvb-items",
                     "phono-items"):
            self.assertIn(f'id="{name}"', html)
        for text in ("Выгрузить список сигналов", "Load from network",
                     "не конвертируются", "состояния"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
