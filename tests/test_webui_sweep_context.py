"""Execute real context handlers with synthetic data, never hardware or user archives."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebSweepContextTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_context_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_sweep_context.cjs"),
                   str(static / "sweep-context.js"), str(static / "platform.js")]
        if base := os.environ.get("ORI_CONTEXT_SMOKE_BASE_URL"):
            command.append(base)
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sweep-context behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/sweep-context.js"'), html.index('src="/static/platform.js"'))
        for name in ("anchor", "before", "after", "run", "clear", "source", "pinned", "status", "summary", "items", "selected", "canvas"):
            self.assertIn(f'id="context-{name}"', html)
        for name in ("spectrum-canvas", "waterfall-canvas", "comparison-canvas", "three-thresholds-canvas",
                     "threshold-canvas", "mask-canvas", "average-overlay", "average-delta"):
            self.assertIn(f'id="{name}"', html)
        for text in ("N1=2 и N2=2", "Автоматический захват N1/N2 при обнаружении не реализован", "Нет привязки к кандидатам", "точный ID (UTF-8)"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
