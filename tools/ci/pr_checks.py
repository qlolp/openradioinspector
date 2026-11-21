"""Select path-dependent PR jobs from complete Git trees and verify their conclusions."""

import argparse
import json
import re
import subprocess
import sys


SHARED_PATHS = (
    ".github/workflows/pr-checks.yml",
    ".github/workflows/conditional-checks.yml",
    "tools/ci/pr_checks.py",
    "tests/test_pr_required_checks.py",
)
PATHS = {
    "clients": (
        "clients/web", "clients/windows", ".github/workflows/modern-clients.yml",
        "radioinspector", "contracts", "crates", "Cargo.lock", "deploy/windows",
        "tools/release/prepare_windows_node.py", "tools/release/native_notices.py",
        "docs/operator-guide", "tools/release/operator_manual.py",
    ),
    "reference": (
        "benchmarks", "crates/ori-core/examples/benchmark_background.rs",
        ".github/workflows/windows-load.yml",
    ),
}
REQUIRED_JOBS = {
    "clients": {"web", "native-bridge", "windows-installer"},
    "reference": {"windows-x64", "linux-arm64"},
}


def changed_suites(base, head):
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
        raise ValueError("Expected full base and head commit IDs from the pull_request event")
    if subprocess.check_output(["git", "rev-parse", "--is-shallow-repository"]).strip() != b"false":
        raise ValueError("PR path selection requires complete Git history")
    ancestors = subprocess.check_output(["git", "merge-base", "--all", base, head], text=True).splitlines()
    if len(ancestors) != 1:
        raise ValueError("PR revisions must have an unambiguous merge base")

    selected = {}
    for suite, paths in PATHS.items():
        # Git compares complete trees. Disabling rename detection covers both the old and new names.
        result = subprocess.run([
            "git", "--literal-pathspecs", "diff", "--quiet", "--no-ext-diff", "--no-renames",
            "--ignore-submodules=none", ancestors[0], head, "--", *SHARED_PATHS, *paths,
        ])
        if result.returncode not in (0, 1):
            result.check_returncode()
        selected[suite] = result.returncode == 1
    return selected


def verify(suite, comparison, selected, result, evidence):
    if comparison != "success":
        raise ValueError(f"PR file comparison concluded {comparison!r}")
    if selected == "false" and result == "skipped":
        print(f"{suite}: no matching files in this PR")
        return
    if selected != "true" or result != "success":
        raise ValueError(f"{suite}: selection={selected!r}, workflow={result!r}")
    expected = dict.fromkeys(REQUIRED_JOBS[suite], "success")
    if json.loads(evidence) != expected:
        raise ValueError(f"{suite}: expected success from {sorted(expected)}, received {evidence}")
    print(f"{suite}: all required jobs passed ({', '.join(sorted(expected))})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    paths = commands.add_parser("paths")
    paths.add_argument("base")
    paths.add_argument("head")
    verdict = commands.add_parser("verify")
    verdict.add_argument("suite", choices=REQUIRED_JOBS)
    verdict.add_argument("comparison")
    verdict.add_argument("selected")
    verdict.add_argument("result")
    verdict.add_argument("evidence")
    args = parser.parse_args()
    if args.command == "paths":
        for suite, selected in changed_suites(args.base, args.head).items():
            print(f"{suite}={str(selected).lower()}")
    else:
        verify(args.suite, args.comparison, args.selected, args.result, args.evidence)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
