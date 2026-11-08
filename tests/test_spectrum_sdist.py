"""Synthetic archive gate regression, not evidence of an actual sdist build."""
import ast
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/release/verify_sdist.py"


class SpectrumSdistTests(unittest.TestCase):
    def test_fixture_is_required_by_archive_gate(self):
        tree = ast.parse(SCRIPT.read_text())
        required = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "required" for t in node.targets)
        )
        fixture = "tests/fixtures/spectrum-presentation-v1.json"
        self.assertIn(fixture, required)
        for omitted in (None, fixture):
            with self.subTest(omitted=omitted), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "synthetic.tar.gz"
                with tarfile.open(archive, "w:gz") as output:
                    for name in sorted(required):
                        if name == omitted:
                            continue
                        data = (ROOT / name).read_bytes()
                        member = tarfile.TarInfo("synthetic/" + name)
                        member.size = len(data)
                        output.addfile(member, io.BytesIO(data))
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), str(archive)],
                    capture_output=True, text=True, check=False,
                )
                if omitted:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(fixture, result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_manifest_includes_json_fixtures(self):
        self.assertIn(
            "recursive-include tests *.py *.cjs *.json",
            (ROOT / "MANIFEST.in").read_text(),
        )
