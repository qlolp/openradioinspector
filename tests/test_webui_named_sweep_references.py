"""Execute production named reference UI with transport and lifecycle races."""
import shutil
import subprocess
import unittest
from pathlib import Path


class WebNamedReferenceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for executable WebUI tests')
    def test_catalog_behavior(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which('node'), str(root / 'tests/webui_named_sweep_references.cjs'),
             str(root / 'radioinspector/web/static/sweep-comparison.js')],
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('named reference behavior checks completed', result.stdout)

    def test_controls_keep_existing_measurement_surfaces(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'radioinspector/web/static/platform.html').read_text(encoding="utf-8")
        for name in ('comparison-name', 'comparison-save', 'comparison-catalog', 'comparison-refresh',
                     'comparison-use', 'comparison-storage', 'comparison-catalog-status', 'comparison-catalog-notice',
                     'comparison-reference-select', 'comparison-pin', 'comparison-clear', 'comparison-run',
                     'spectrum-canvas', 'waterfall-canvas'):
            self.assertIn(f'id="{name}"', html)
        self.assertNotIn('maxlength="80"', html, 'HTML maxlength counts UTF-16 units, not Unicode code points')


if __name__ == '__main__':
    unittest.main()
