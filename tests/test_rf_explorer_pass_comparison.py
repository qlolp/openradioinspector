"""Cross-check display selectors against a complete independent archive export."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.test_rf_explorer_survey_export import HEADER, PASS_ID, START, STEP, sweep
from tools.rf_explorer.export_survey import export


class PanoramaComparisonTests(unittest.TestCase):
    def test_later_overlap_winner_last_bin_and_original_identity(self):
        from tools.rf_explorer.compare_panorama import verify

        with TemporaryDirectory() as folder:
            root = Path(folder)
            a, b = sweep(1), sweep(2)
            # In this pass window two starts at grid index 3; both tiles measured that frequency.
            b['measurement']['levels'][0] = -5
            b['measurement']['levels'][-1] = -4
            source = root/'session.ndjson'
            source.write_text('\n'.join(map(json.dumps, [HEADER, a, b]))+'\n')
            csv = root/'pass.csv'
            meta = export(source, PASS_ID, csv, root/'pass.svg')
            raw = b['measurement']
            ts = raw['interval']
            def point(i):
                return dict(frequency_hz=raw['frequencies_hz'][i], level_dbm=raw['levels'][i],
                            tile=2, sweep_id=raw['id'], point_index=i, last_measured_at=ts['ended_at'], **ts)
            summary = {**meta, 'expected_points':115, 'received_tiles':2, 'complete':True, 'duration_s':meta['wall_seconds']}
            data = {'summary':summary, 'columns':[point(0),point(111)],
                    'coverage':[{'start_hz':START,'stop_hz':START+114*STEP}]}
            check = verify(csv, data)
            self.assertEqual((check['unique_frequencies'], check['repeat_observations'], check['display_columns']), (115,109,2))
            data['columns'][0]['sweep_id'] = a['measurement']['id']
            with self.assertRaisesRegex(ValueError, 'sweep_id'):
                verify(csv, data)
