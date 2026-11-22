import io
import json
import unittest

from tools.field.verify_rf_pass import verify


class FieldCoverageTests(unittest.TestCase):
    def document(self, count, corrupt=False, drop=False):
        a = 100_000_000
        b = a + (count - 1) * 10_000
        n = (count + 111) // 112
        records = [dict(format="openradioinspector-archive-ndjson", format_version=1, session_id="s")]
        for tile in range(1, n + 1 - int(drop)):
            start = min(a + (tile - 1) * 1_120_000, b - 1_110_000)
            freq = [start + i * 10_000 for i in range(112)]
            if corrupt and tile == 2:
                freq[36] = freq[35]
            survey = dict(start_hz=a, stop_hz=b, step_hz=10_000, pass_id="run", tile=tile, tiles=n)
            records.append(dict(kind="sweep", measurement=dict(session_id="s", stream_id="r",
                id=f"sweep-{tile}", frequencies_hz=freq, levels=[-65.0]*112, level_unit="dBm",
                settings=dict(survey=survey))))
        data = b"".join(json.dumps(r).encode() + b"\n" for r in records)
        return io.BytesIO(data)

    def test_real_grid_dimensions(self):
        for count, tiles, overlaps in [(30001, 268, 15), (268501, 2398, 75)]:
            got = verify(self.document(count), "r", "run", 100_000_000, 100_000_000+(count-1)*10000)
            self.assertEqual((got["frequencies"], got["windows"], got["overlap_points"]), (count, tiles, overlaps))

    def test_interior_hole_and_incomplete_pass(self):
        for kw in [dict(corrupt=True), dict(drop=True)]:
            with self.assertRaises(ValueError):
                verify(self.document(30001, **kw), "r", "run", 100000000, 400000000)
