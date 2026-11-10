"""Decimation includes all source bins and preserves a peak in the tail."""
import unittest
import numpy as np
from radioinspector.trace import Trace


class DecimationTailTests(unittest.TestCase):
    def test_peak_in_partial_last_bucket_is_preserved(self):
        trace = Trace(np.arange(11), [-90] * 10 + [0])
        result = trace.decimate(5)
        np.testing.assert_array_equal(result.frequencies, [0, 3, 6, 10])
        self.assertEqual(result.peak, 0)
        self.assertEqual(trace.levels[-1], 0)

    def test_all_lengths_targets_and_peak_positions_preserve_global_peak(self):
        for size in range(1, 31):
            for target in range(1, size + 2):
                for peak in {0, size // 2, size - 1}:
                    with self.subTest(size=size, target=target, peak=peak):
                        levels = np.full(size, -90.)
                        levels[peak] = -10
                        result = Trace(np.arange(size), levels).decimate(target)
                        self.assertLessEqual(result.size, target)
                        self.assertEqual(result.peak, -10)
                        self.assertTrue(np.all(np.diff(result.frequencies) > 0))

    def test_ties_use_first_source_bin_and_noop_copies(self):
        result = Trace(np.arange(11), [-20] * 11).decimate(5)
        np.testing.assert_array_equal(result.frequencies, [0, 3, 6, 9])
        original = Trace([1, 2], [-90, -80], 'dBuV')
        result = original.decimate(5)
        result.levels[0] = 0
        self.assertEqual(original.levels[0], -90)
        self.assertEqual(result.unit, 'dBuV')
        self.assertTrue(Trace([], []).decimate(1).empty)
        for target in (0, -1):
            with self.assertRaises(ValueError):
                original.decimate(target)
