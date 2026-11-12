"""Real autocrlf checkout preserves canonical hashes and opaque fixture bytes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DETECTOR = "radioinspector/automatic_detection.py"
TEXT_EXTENSIONS = {
    ".py", ".json", ".rs", ".toml", ".ts", ".tsx", ".swift", ".proto", ".js", ".cjs", ".mjs",
    ".css", ".html", ".yml", ".yaml", ".md", ".sh", ".ps1", ".sql", ".lock", ".in", ".txt",
    ".csv", ".iss", ".service", ".plist", ".pbxproj", ".xcscheme", ".xcworkspacedata",
}
BINARY_EXTENSIONS = {".sqlite", ".sqlite3", ".db", ".ico"}


def opaque(path):
    return any(part.lower() == "fixtures" for part in Path(path).parts[:-1]) or Path(path).suffix in BINARY_EXTENSIONS


def git(directory, *arguments, input=None):
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(["git", "-C", str(directory), *arguments], input=input,
                            capture_output=True, env=environment, timeout=30)
    if result.returncode:
        raise AssertionError(f"git {arguments[0]} failed: {result.stderr.decode('utf-8', 'replace')}")
    return result.stdout


class RepositoryCheckoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        cls.repository = root / "repository"
        cls.checkout = root / "checkout"
        cls.repository.mkdir()
        cls.checkout.mkdir()
        cls.paths = [p.decode("utf-8") for p in git(ROOT, "ls-files", "-z").split(b"\0") if p]
        cls.raw_paths = [path for path in cls.paths if opaque(path)]
        selected = {DETECTOR}
        for extension in TEXT_EXTENSIONS:
            candidates = [path for path in cls.paths if Path(path).suffix == extension and not opaque(path)]
            if candidates:
                selected.add(sorted(candidates)[0])
        cls.text_paths = sorted(selected)
        cls.expected = {path: git(ROOT, "show", "HEAD:" + path) for path in sorted(selected | set(cls.raw_paths))}
        # Future fixture payloads may contain meaningful CRLF and lone CR bytes.
        cls.expected["tests/fixtures/opaque-checkout-probe.json"] = b'{"wire":"exact"}\r\nline\rnext\n'
        cls.expected["tests/fixtures/opaque-checkout-probe.sqlite"] = b'SQLite format 3\0\r\n\x00\xffraw\r\n'
        for path, value in cls.expected.items():
            destination = cls.repository / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
        attributes = ROOT / ".gitattributes"
        if attributes.exists():
            (cls.repository / ".gitattributes").write_bytes(attributes.read_bytes())
        git(cls.repository, "-c", "init.defaultBranch=checkout-proof", "init", "--quiet")
        git(cls.repository, "config", "core.autocrlf", "false")
        git(cls.repository, "config", "core.safecrlf", "false")
        git(cls.repository, "add", "--all")
        git(cls.repository, "-c", "core.autocrlf=true", "checkout-index", "--all", "--force",
            "--prefix=" + str(cls.checkout) + os.sep)

    def test_autocrlf_checkout_preserves_the_frozen_detector_hash_and_generated_text(self):
        fixture = json.loads((ROOT / "tests/fixtures/automatic-detection-science-v1.json").read_text())
        self.assertEqual(hashlib.sha256((self.checkout / DETECTOR).read_bytes()).hexdigest(),
                         fixture["oracle_source_sha256"])
        for path in self.text_paths:
            with self.subTest(path=path):
                self.assertTrue((self.checkout / path).read_bytes() == self.expected[path],
                                f"canonical source/generated text bytes changed: {path}")

    def test_autocrlf_checkout_preserves_actual_raw_fixtures_and_binary_archives_byte_for_byte(self):
        for path in self.expected:
            if opaque(path):
                with self.subTest(path=path):
                    self.assertTrue((self.checkout / path).read_bytes() == self.expected[path],
                                    f"opaque fixture bytes changed: {path}")

    def test_attributes_cover_the_actual_tracked_text_and_opaque_fixture_inventory(self):
        paths = [path for path in self.paths if opaque(path) or Path(path).suffix in TEXT_EXTENSIONS]
        output = git(self.repository, "check-attr", "-z", "--stdin", "text", "eol",
                     input=b"\0".join(path.encode("utf-8") for path in paths) + b"\0").split(b"\0")
        attributes = {}
        for position in range(0, len(output) - 1, 3):
            path, name, value = output[position:position + 3]
            attributes.setdefault(path.decode("utf-8"), {})[name.decode()] = value.decode()
        for path in paths:
            with self.subTest(path=path):
                if opaque(path):
                    self.assertEqual(attributes[path]["text"], "unset")
                else:
                    self.assertEqual(attributes[path], {"text": "set", "eol": "lf"})
