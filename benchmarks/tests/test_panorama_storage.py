import unittest

from benchmarks.ori_bench.panorama_storage import compare, rc_checks


def report():
    formats = []
    for schema, size in [(6, 1000), (7, 700)]:
        formats.append({
            "schema": schema, "logical_growth_bytes": size, "source_sha256": "a" * 64,
            "after_checkpoint": {"main_bytes": size, "wal_bytes": 0},
            "archive_counts": {"sweeps": 512, "sweep_events": 512, "complete_grids": 512},
        })
    return {"writer": "rust", "workload": {"sweeps": 512, "points_per_sweep": 290,
            "batch": 8, "seed": 42}, "full": formats[0], "compact": formats[1]}


class PanoramaStorageTest(unittest.TestCase):
    def test_rc_accepts_exactly_thirty_percent_for_both_supported_grids(self):
        for points in (290, 4096):
            result = report()
            result["workload"]["points_per_sweep"] = points
            self.assertTrue(all(rc_checks(result).values()))

    def test_rc_rejects_missing_data_wrong_fixture_and_insufficient_savings(self):
        for path, invalid in [
            (("writer",), "python"), (("workload", "points_per_sweep"), 1024),
            (("workload", "batch"), 1), (("workload", "seed"), 1),
            (("full", "archive_counts", "sweeps"), 511),
            (("compact", "archive_counts", "complete_grids"), 511),
            (("compact", "archive_counts", "sweep_events"), 511),
            (("compact", "source_sha256"), "b" * 64),
            (("compact", "after_checkpoint", "wal_bytes"), 1),
            (("compact", "after_checkpoint", "main_bytes"), 701),
            (("compact", "logical_growth_bytes"), 701),
            (("full", "logical_growth_bytes"), 0), (("compact", "schema"), 6),
        ]:
            with self.subTest(path=path):
                result = report()
                node = result
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = invalid
                self.assertFalse(all(rc_checks(result).values()))

    def test_counts_are_read_back_from_both_persisted_archives(self):
        result = compare(sweeps=3, points=5, batch=2)
        self.assertEqual(result["full"]["source_sha256"], result["compact"]["source_sha256"])
        for name in ("full", "compact"):
            self.assertEqual(result[name]["archive_counts"],
                             {"sweeps": 3, "sweep_events": 3, "complete_grids": 3})
