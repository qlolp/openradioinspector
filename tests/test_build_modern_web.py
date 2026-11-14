"""The packaged-UI preflight reads completed assets without rebuilding them."""
import hashlib
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from tools import build_modern_web


class ModernWebInputTests(unittest.TestCase):
    def package_fixture(self, root):
        source = root / "vite-output"
        (source / "assets").mkdir(parents=True)
        (source / "index.html").write_text('<script src="/modern/assets/app.js"></script>\n')
        (source / "assets/app.js").write_text('console.log("offline fixture");\n')
        destination = root / "packaged"
        build_modern_web.package(source, destination)
        return destination

    def snapshot(self, root):
        return {
            path.relative_to(root).as_posix(): (
                stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            )
            for path in [root, *root.rglob("*")]
        }

    def check(self, destination, *extra):
        return subprocess.run(
            [sys.executable, str(Path(build_modern_web.__file__)), "--check",
             "--destination", str(destination), *map(str, extra)],
            capture_output=True, text=True, timeout=10,
        )

    def assert_preparation_error(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("npm --prefix clients/web run build", result.stderr)
        self.assertIn("python tools/build_modern_web.py", result.stderr)

    def test_complete_package_is_checked_without_source_or_file_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            destination = self.package_fixture(root)
            (root / "unrelated-marker").write_bytes(b"keep exactly")
            before = self.snapshot(root)
            result = self.check(destination, "--source", root / "source-not-installed")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.snapshot(root), before)

    def test_missing_package_fails_with_build_commands_without_creating_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            before = self.snapshot(root)
            result = self.check(root / "missing-package")
            self.assert_preparation_error(result)
            self.assertEqual(self.snapshot(root), before)

    def test_incomplete_package_fails_without_repair_or_replacement(self):
        for damage in ("index", "manifest", "listed-asset", "invalid-manifest", "unlisted-file", "unlisted-nested-manifest"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                destination = self.package_fixture(root)
                if damage == "index":
                    (destination / "index.html").unlink()
                elif damage == "manifest":
                    (destination / "modern-build.json").unlink()
                elif damage == "listed-asset":
                    (destination / "assets/app.js").unlink()
                elif damage == "invalid-manifest":
                    (destination / "modern-build.json").write_text("{broken json")
                elif damage == "unlisted-file":
                    (destination / "unlisted.js").write_text("not in the completed build")
                else:
                    (destination / "assets/modern-build.json").write_text("{}")
                before = self.snapshot(root)
                result = self.check(destination)
                self.assert_preparation_error(result)
                self.assertEqual(self.snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
