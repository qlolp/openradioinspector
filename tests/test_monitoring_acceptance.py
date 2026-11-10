"""Synthetic acceptance checks; no instrument or RF validation is implied."""
import tempfile
import contextlib
import io
import unittest
from pathlib import Path
from tests.monitoring_endurance import compare_detectors, fixture, main, run_acceptance


class MonitoringAcceptanceTests(unittest.TestCase):
    def test_report_cannot_overwrite_the_measurement_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.sqlite'
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['--report', str(path), '--archive', str(path)])
            self.assertFalse(path.exists())

    def test_seeded_fixture_keeps_gap_and_incompatible_source_explicit(self):
        self.assertEqual(fixture(40, 17, 's', 'a').levels, fixture(40, 17, 's', 'a').levels)
        self.assertIsNone(fixture(122, 17, 's', 'a'))
        self.assertEqual(fixture(239, 17, 's', 'a', incompatible=True).settings['rbw_hz'], 30000)

    def test_actual_legacy_threshold_is_compared_at_equal_weak_event_recall(self):
        result = compare_detectors(seed=17, slots=240)
        old, new = result['legacy_fixed_threshold'], result['robust_city']
        self.assertEqual(old['weak']['recall'], 1.0)
        self.assertEqual(new['weak']['recall'], 1.0)
        self.assertEqual(new['impulse']['recall'], 1.0)
        self.assertEqual(new['broad_scene']['recall'], 1.0)
        self.assertGreater(old['false_positive_frames'], new['false_positive_frames'])
        self.assertTrue(result['comparable_weak_recall'])
        self.assertEqual(result['hardware_validation'], 'not_run')

    def test_real_archive_pages_restart_and_bounded_registry_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_acceptance(Path(directory) / 'archive.sqlite', seed=17, slots=240)
        self.assertEqual(report['raw_sweeps'], 235)
        self.assertEqual(report['gap_slots'], 5)
        self.assertEqual(report['quality_records'], 1)
        self.assertEqual(report['raw_verified'], 235)
        self.assertEqual(report['incompatible_sources_rejected'], 1)
        self.assertTrue(report['restart_no_duplicate_events'])
        self.assertTrue(report['bounded_working_registry'])
        self.assertGreater(report['core_peak_memory']['python_sampled_tracemalloc_peak_bytes'], 0)
        self.assertEqual(report['hardware_validation'], 'not_run')


if __name__ == '__main__':
    unittest.main()
