"""Ownership selection for native CI diagnostics; no subprocesses or devices."""
import unittest

from native_ci import Process, descendants, parse_process_table, sampling_candidates


class NativeDiagnosticOwnershipTests(unittest.TestCase):
    def test_process_identity_preserves_start_time_and_spaces_without_command_arguments(self):
        values = parse_process_table(" 101 100 Wed Nov 11 09:00:00 2026 /Applications/Xcode 27.app/xctest\ninvalid row\n")
        self.assertEqual(values[101], Process(101, 100, "Wed Nov 11 09:00:00 2026", "/Applications/Xcode 27.app/xctest"))

    def test_only_live_descendants_of_the_known_child_are_eligible(self):
        values = {p.pid: p for p in [Process(100, 1, "a", "python"), Process(101, 100, "b", "swift"),
                                    Process(102, 101, "c", "xctest"), Process(103, 1, "d", "xctest"),
                                    Process(104, 105, "e", "xctest"), Process(105, 104, "f", "swift")]}
        self.assertEqual({p.pid for p in descendants(100, values)}, {101, 102})
        self.assertEqual(descendants(999, values), [])

    def test_stalled_owned_xctest_is_sampled_once_and_pid_reuse_restarts_its_clock(self):
        values = {100: Process(100, 1, "a", "python"), 101: Process(101, 100, "b", "xctest")}
        seen, sampled = {}, set()
        self.assertEqual(sampling_candidates(100, values, seen, sampled, now=0), [])
        self.assertEqual(sampling_candidates(100, values, seen, sampled, now=59), [])
        self.assertEqual([p.pid for p in sampling_candidates(100, values, seen, sampled, now=60)], [101])
        sampled.add(values[101])
        self.assertEqual(sampling_candidates(100, values, seen, sampled, now=61), [])
        values[101] = Process(101, 100, "new-start", "xctest")
        self.assertEqual(sampling_candidates(100, values, seen, sampled, now=62), [])
        self.assertEqual([p.pid for p in sampling_candidates(100, values, seen, sampled, now=122)], [101])


if __name__ == "__main__":
    unittest.main()
