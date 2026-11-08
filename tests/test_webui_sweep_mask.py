"""Execute mask UI handlers without hardware or private archive data."""

import shutil
import subprocess
import unittest
from pathlib import Path


class WebSweepMaskTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_mask_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests/webui_sweep_mask.cjs"),
             str(static / "sweep-mask.js"), str(static / "platform.js")],
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mask behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="/static/sweep-mask.js"'), html.index('src="/static/platform.js"'))
        for name in ("mask-canvas", "mask-editor", "mask-file", "mask-run", "mask-template", "mask-save", "mask-regions",
                     "spectrum-canvas", "waterfall-canvas", "comparison-canvas", "comparison-catalog", "threshold-canvas"):
            self.assertIn(f'id="{name}"', html)
        self.assertIn("НЕНОРМАТИВНЫЙ пример", html)
        self.assertIn("Сохранить проверенную маску", html)
        self.assertIn("Черновик не проверен ядром", html)



if __name__ == "__main__":
    unittest.main()
