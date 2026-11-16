"""Include portable acceptance regressions in the existing release runner."""
from pathlib import Path
import unittest


def load_tests(loader,tests,pattern):
    root=Path(__file__).resolve().parents[1]
    return loader.loadTestsFromNames(['benchmarks.tests.'+path.stem for path in sorted((root/'benchmarks/tests').glob('test_mac_acceptance_*.py'))])
