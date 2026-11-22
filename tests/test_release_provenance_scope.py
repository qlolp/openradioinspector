import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.release.check_public_tree import violations


class ProvenanceScopeTests(unittest.TestCase):
    def test_reports_only_history_reachable_from_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (repo / "sample").write_text("first")
            git("add", "sample")
            git("commit", "-qm", "first")
            first = git("rev-parse", "HEAD")
            git("checkout", "-qb", "unreleased")
            (repo / "sample").write_text("test-only blocked blob")
            git("commit", "-qam", "second")
            blocked = {git("rev-parse", "HEAD:sample")}
            git("rm", "-q", "sample")
            git("commit", "-qm", "clean tip")
            self.assertEqual(violations(repo, blocked, revision="HEAD"), [])
            self.assertEqual(violations(repo, blocked, history_from=first), [])
            self.assertEqual(violations(repo, blocked, history_from="HEAD"), violations(repo, blocked, history=True))
            self.assertEqual(len(violations(repo, blocked, history_from="HEAD")), 1)
