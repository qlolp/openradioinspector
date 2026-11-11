"""Probe the selected interpreter/display tools before requesting apt packages."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import shutil
import subprocess
import sys


class BootstrapError(RuntimeError):
    """Required headless dependencies remain unavailable."""


@dataclass(frozen=True)
class Probe:
    packages: tuple[str, ...]
    problems: tuple[str, ...]

    @property
    def ready(self):
        return not self.problems


def probe_headless(*, executable=sys.executable, which=shutil.which,
                   run=subprocess.run):
    packages = set()
    problems = []
    checks = [("selected tkinter", "python3-tk",
               [executable, "-I", "-c", "import tkinter; print(tkinter.TkVersion)"])]
    for name, package, option in (("xvfb-run", "xvfb", "--help"),
                                  ("Xvfb", "xvfb", "-help"),
                                  ("xauth", "xauth", "-V")):
        path = which(name)
        if path:
            checks.append((name, package, [path, option]))
        else:
            packages.add(package)
            problems.append(name + " unavailable")
    for name, package, command in checks:
        try:
            result = run(command, check=False, capture_output=True, text=True,
                         timeout=10)
            good = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            good = False
        if not good:
            packages.add(package)
            problems.append(name + " probe failed")
    return Probe(tuple(sorted(packages)), tuple(problems))


def ensure_headless(*, executable=sys.executable, which=shutil.which,
                    run=subprocess.run, install=True):
    result = probe_headless(executable=executable, which=which, run=run)
    if result.ready:
        return result
    if not install:
        raise BootstrapError("; ".join(result.problems))
    # Exactly one installation attempt. Package-manager/network failures must
    # fail the job, and apt's distro tkinter is never accepted as evidence for
    # setup-python's independently selected interpreter.
    run(["sudo", "apt-get", "update"], check=True, timeout=180)
    run(["sudo", "apt-get", "install", "-y", *result.packages],
        check=True, timeout=180)
    result = probe_headless(executable=executable, which=which, run=run)
    if not result.ready:
        raise BootstrapError("; ".join(result.problems))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    ensure_headless(install=not args.check_only)
    print("Selected Python tkinter and headless display tools are ready.")


if __name__ == "__main__":
    main()
