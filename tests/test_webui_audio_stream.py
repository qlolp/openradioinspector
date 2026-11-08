"""Execute real audio-stream handlers with synthetic data, never user archives."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class WebAudioStreamTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for executable WebUI tests")
    def test_audio_stream_behavior(self):
        root = Path(__file__).resolve().parents[1]
        static = root / "radioinspector/web/static"
        command = [shutil.which("node"), str(root / "tests/webui_audio_stream.cjs"),
                   str(static / "audio-stream.js"), str(static / "platform.js")]
        if base := os.environ.get("ORI_AUDIO_SMOKE_BASE_URL"):
            command.append(base)
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("audio-stream behavior checks completed", result.stdout)

    def test_panel_assets_and_existing_charts(self):
        static = Path(__file__).resolve().parents[1] / "radioinspector/web/static"
        html = (static / "platform.html").read_text()
        self.assertLess(html.index('src="/static/audio-stream.js"'), html.index('src="/static/platform.js"'))
        for name in ("connect", "disconnect", "refresh", "stream", "source", "status", "summary", "items"):
            self.assertIn(f'id="audio-{name}"', html)
        for name in ("spectrum-canvas", "waterfall-canvas", "comparison-canvas", "three-thresholds-canvas",
                     "threshold-canvas", "mask-canvas", "average-overlay", "average-delta",
                     "context-canvas", "raster-items", "meas-items", "counter-items", "dvb-items",
                     "phono-items", "siglist-items", "map-items"):
            self.assertIn(f'id="{name}"', html)
        for text in ("трансляция демодулированного сигнала", "PCM16LE",
                     "Панель не генерирует аудио", "запись и другие клиенты не блокируются"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
