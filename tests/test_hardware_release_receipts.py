"""Five independent physical configurations must qualify, even on an already built platform."""
import copy
import unittest

from tests.test_signed_release import HASH, NATIVE_CI, REV, receipt
from tools.release import bench_receipt, release_acceptance as gate


class HardwareReleaseReceiptTests(unittest.TestCase):
    def validate(self, row, **options):
        gate.validate_receipt(row, row['platform'], REV, HASH, HASH, 42, native_ci=NATIVE_CI, **options)

    def test_all_new_platform_receipts_require_three_solo_and_two_paired_shifts(self):
        for platform in ('macos', 'windows', 'pi', 'linux-amd64'):
            with self.subTest(platform=platform):
                row = receipt(platform)
                try:
                    self.validate(row)
                except ValueError as error:
                    self.fail(f'five complete shifts should qualify: {error}')
                self.assertEqual(set(bench_receipt.template(platform, REV, HASH, HASH, 42,
                                                           native_ci=NATIVE_CI)['hardware_runs']),
                                 {'tinysa', 'rf-explorer', 'ardv1', 'tinysa-ardv1', 'rf-explorer-ardv1'})
                for config in row['hardware_runs']:
                    spoiled = copy.deepcopy(row)
                    del spoiled['hardware_runs'][config]
                    with self.assertRaises(ValueError):
                        self.validate(spoiled)

    def test_legacy_can_be_read_for_history_but_cannot_authorize_any_publication(self):
        for platform in ('macos', 'windows', 'pi', 'linux-amd64'):
            row = receipt(platform, version=1)
            with self.subTest(platform=platform):
                with self.assertRaises(ValueError):
                    self.validate(row)
                self.validate(row, historical=True)
                row['schema_version'] = 2
                with self.assertRaises(ValueError):
                    self.validate(row, historical=True)

    def test_a_spare_profile_another_session_a_short_run_wrong_units_or_digest_are_refused(self):
        row = receipt('pi')
        for path, value in (
            (('extra',), {}), (('ardv1', 'physical'), 1), (('tinysa', 'seconds'), 28799),
            (('tinysa', 'seconds'), True), (('rf-explorer', 'unexplained_losses'), 1),
            (('rf-explorer', 'duplicates'), False), (('ardv1', 'sources', 'ardv1', 'unit'), 'dBm'),
            (('ardv1', 'sources', 'ardv1', 'kind'), 'sweep'),
            (('tinysa', 'sources', 'tinysa', 'records'), 1), (('tinysa', 'sources', 'tinysa', 'ids_sha256'), ''),
            (('tinysa-ardv1', 'sources', 'ardv1', 'records'), 4311),
            (('rf-explorer-ardv1', 'report_sha256'), row['hardware']['report_sha256']),
            (('rf-explorer', 'coverage_sha256'), row['hardware_runs']['rf-explorer-ardv1']['coverage_sha256']),
            (('ardv1', 'session_sha256'), row['hardware_runs']['tinysa']['session_sha256']),
            (('ardv1', 'sources', 'ardv1', 'ids_sha256'), row['hardware_runs']['tinysa-ardv1']['sources']['ardv1']['ids_sha256']),
            (('rf-explorer-ardv1', 'sources'), row['hardware_runs']['tinysa-ardv1']['sources']),
        ):
            with self.subTest(path=path):
                spoiled = copy.deepcopy(row)
                obj = spoiled['hardware_runs']
                for key in path[:-1]:
                    obj = obj[key]
                obj[path[-1]] = value
                with self.assertRaises(ValueError):
                    self.validate(spoiled)
