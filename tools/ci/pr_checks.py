"""Select path-dependent PR jobs from complete Git trees and verify their conclusions."""

import argparse
import json
import os
import re
import subprocess
import sys


SHARED_PATHS = (
    ".github/workflows/pr-checks.yml",
    ".github/workflows/conditional-checks.yml",
    "tools/ci/pr_checks.py",
    "tests/test_pr_required_checks.py",
)
# Both native builds compile the workspace and embed the Vite client.
NATIVE_INPUTS = (
    ".gitattributes", ".cargo/**", "Cargo.toml", "Cargo.lock", "rust-toolchain.toml",
    "clients/web/**", "contracts/**", "crates/**", "tools/build_modern_web.py",
)
OPERATOR_GUIDE = "docs/operator-guide/**"
PATHS = {
    "clients": NATIVE_INPUTS + (
        ".github/workflows/modern-clients.yml", "clients/windows/**", "radioinspector/**", "deploy/windows/**",
        ".gitignore", "pyproject.toml", "MANIFEST.in", "LICENSE", "rustfmt.toml", "tests/fixtures/field-strength-rms-v2.json",
        "tools/release/prepare_windows_node.py", "tools/release/native_notices.py",
        "tools/release/operator_manual.py", OPERATOR_GUIDE,
    ),
    "reference": NATIVE_INPUTS + (
        "radioinspector/web/static/**", "radioinspector/web/modern/**", "radioinspector/monitoring/storage_window.sql",
        ".github/workflows/windows-load.yml", "benchmarks/**", "tools/modernization/api_parity.py",
        "tests/__init__.py", "tests/loopback_dns.py", "tests/release_process_ownership.py", "tests/run_release_checks.py",
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

    # Git returns every changed path, including both sides of a rename, without API pagination.
    names = subprocess.check_output([
        "git", "diff", "--name-only", "-z", "--no-ext-diff", "--no-renames",
        "--ignore-submodules=none", ancestors[0], head, "--",
    ])
    if names and not names.endswith(b"\0"):
        raise ValueError("Incomplete Git path list")
    selected = dict.fromkeys(PATHS, False)
    # Match literal file names and directory prefixes. Only the installed guide includes Markdown.
    files = {
        suite: set(SHARED_PATHS) | {p for p in paths if not p.endswith("/**")}
        for suite, paths in PATHS.items()
    }
    directories = {
        suite: tuple(p.removesuffix("**") for p in paths if p.endswith("/**"))
        for suite, paths in PATHS.items()
    }
    for raw in names.split(b"\0")[:-1]:
        path = os.fsdecode(raw)
        if path.endswith(".md") and not path.startswith(OPERATOR_GUIDE.removesuffix("**")):
            continue
        for suite in PATHS:
            if path in files[suite] or path.startswith(directories[suite]):
                selected[suite] = True
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
