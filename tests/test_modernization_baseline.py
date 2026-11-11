"""The benchmark must count offered sources and verify the actual archive."""
import importlib.util
import tempfile
import unittest
from pathlib import Path


class ModernizationBaselineTests(unittest.TestCase):
    def runner(self):
        self.assertIsNotNone(importlib.util.find_spec('tests.modernization_baseline'),
                             'The reproducible modernization benchmark is missing')
        from tests.modernization_baseline import run
        return run

    def test_small_run_reports_all_offered_sources_and_runtime(self):
        run = self.runner()
        with tempfile.TemporaryDirectory() as temporary:
            report = run(Path(temporary), sweeps=7, bins=4)
            self.assertEqual(report['sources'], {'offered': 7, 'persisted': 7, 'verified': 7})
            self.assertEqual(report['workload']['bins'], 4)
            self.assertTrue(report['raw_preserved'])
            self.assertGreater(report['archive_bytes'], 0)
            self.assertIn('sqlite', report['runtime'])
            self.assertIn('python', report['runtime'])
            self.assertGreaterEqual(report['commit_latency_ms']['p95'], 0)
            self.assertFalse(report['hardware_tested'])
            self.assertFalse(report['ui_tested'])

    def test_existing_archive_cannot_be_reused_or_modified(self):
        run = self.runner()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            run(directory, sweeps=1, bins=2)
            before = (directory / 'archive.sqlite').read_bytes()
            with self.assertRaises(FileExistsError):
                run(directory, sweeps=1, bins=2)
            self.assertEqual((directory / 'archive.sqlite').read_bytes(), before)

    def test_invalid_workload_does_not_create_an_archive(self):
        run = self.runner()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for count, bins in ((0, 2), (1, 0), (True, 2), (1, 1_000_001)):
                with self.subTest(count=count, bins=bins), self.assertRaises(ValueError):
                    run(directory, sweeps=count, bins=bins)
            self.assertFalse((directory / 'archive.sqlite').exists())
