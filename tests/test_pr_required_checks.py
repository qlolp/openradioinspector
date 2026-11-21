"""Exercise the Git comparison and the PR verdict with Actions job conclusions."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKS = ROOT / "tools" / "ci" / "pr_checks.py"


def check(*args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, str(CHECKS), *args], cwd=cwd, text=True, capture_output=True
    )


class ChangeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "-q")
        self.git("config", "user.name", "CI test")
        self.git("config", "user.email", "ci@example.invalid")
        self.blob = self.git("hash-object", "-w", "--stdin", data="content")

    def git(self, *args, data=""):
        return subprocess.run(
            ["git", *args], cwd=self.repo, input=data, text=True, capture_output=True, check=True
        ).stdout.strip()

    def commit(self, paths, parent=None):
        self.git("read-tree", "--empty")
        entries = "".join(f"100644 {self.blob}\t{path}\0" for path in paths)
        self.git("update-index", "-z", "--index-info", data=entries)
        tree = self.git("write-tree")
        return self.git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", "fixture")

    def selections(self, base, head):
        result = check("paths", base, head, cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=") for line in result.stdout.splitlines())

    def test_docs_only_pr_uses_the_merge_base_even_after_main_changes_clients(self):
        common = self.commit(["README.md"])
        base = self.commit(["README.md", "clients/web/main.ts"], common)
        head = self.commit(["README.md", "docs/release/status.md"], common)
        self.assertEqual(self.selections(base, head), {"clients": "false", "reference": "false"})

    def test_a_relevant_file_after_three_thousand_docs_is_still_selected(self):
        base = self.commit([])
        head = self.commit(
            [f"docs/{index:04}.md" for index in range(3001)] + ["radioinspector/worker.py"], base
        )
        self.assertEqual(self.selections(base, head), {"clients": "true", "reference": "false"})

    def test_renames_and_deletions_include_the_old_scope(self):
        base = self.commit(["benchmarks/old.py"])
        head = self.commit(["docs/new.py"], base)
        self.assertEqual(self.selections(base, head), {"clients": "false", "reference": "true"})

    def test_the_selector_itself_exercises_both_scopes(self):
        base = self.commit([])
        head = self.commit(["tools/ci/pr_checks.py"], base)
        self.assertEqual(self.selections(base, head), {"clients": "true", "reference": "true"})

    def test_unknown_revision_and_incomplete_history_cannot_certify_a_skip(self):
        base = self.commit([])
        head = self.commit(["docs/guide.md"], base)
        self.git("update-ref", "refs/heads/main", head)
        shallow = self.repo / "shallow-copy"
        self.git("clone", "-q", "--depth=1", "--branch", "main", self.repo.as_uri(), str(shallow))
        for cwd, older, newer in ((self.repo, "0" * 40, head), (shallow, head, head)):
            with self.subTest(repository=cwd):
                result = check("paths", older, newer, cwd=cwd)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


class RequiredVerdictTests(unittest.TestCase):
    suites = {
        "clients": ("web", "native-bridge", "windows-installer"),
        "reference": ("windows-x64", "linux-arm64"),
    }

    def verdict(self, suite, comparison, selected, outcome, evidence):
        return check("verify", suite, comparison, selected, outcome, json.dumps(evidence))

    def test_only_a_successful_comparison_can_exempt_a_suite(self):
        for suite in self.suites:
            allowed = self.verdict(suite, "success", "false", "skipped", {})
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            for comparison, selected, outcome in (
                ("failure", "false", "skipped"),
                ("skipped", "false", "skipped"),
                ("cancelled", "false", "skipped"),
                ("success", "", "skipped"),
                ("success", "false", "failure"),
            ):
                with self.subTest(suite=suite, comparison=comparison, selected=selected, outcome=outcome):
                    self.assertNotEqual(self.verdict(suite, comparison, selected, outcome, {}).returncode, 0)

    def test_selected_suites_require_success_from_every_named_job(self):
        for suite, jobs in self.suites.items():
            successful = dict.fromkeys(jobs, "success")
            allowed = self.verdict(suite, "success", "true", "success", successful)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            for job in jobs:
                for conclusion in ("failure", "skipped", "cancelled", None):
                    evidence = successful.copy()
                    if conclusion is None:
                        del evidence[job]
                    else:
                        evidence[job] = conclusion
                    with self.subTest(suite=suite, job=job, conclusion=conclusion):
                        self.assertNotEqual(self.verdict(suite, "success", "true", "success", evidence).returncode, 0)
            for conclusion in ("skipped", "failure", "cancelled"):
                with self.subTest(suite=suite, conclusion=conclusion):
                    self.assertNotEqual(self.verdict(suite, "success", "true", conclusion, successful).returncode, 0)
