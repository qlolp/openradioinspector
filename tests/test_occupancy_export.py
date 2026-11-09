import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radioinspector.occupancy_export import export_csv, parse_artifact, main, MAX_ARTIFACT_BYTES


def fixture():
    data = json.loads((Path(__file__).parent/'fixtures/sweep-occupancy-v1.json').read_text())['data']
    return {'format':'openradioinspector-sweep-occupancy','format_version':1,'data':data}


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


class OccupancyExportTests(unittest.TestCase):
    def test_exact_bins_provenance_unknown_airtime_and_hash(self):
        raw = encode(fixture())
        rows = list(csv.DictReader(io.StringIO(export_csv(raw))))
        self.assertEqual([r['frequency_hz'] for r in rows], ['100','101','102'])
        self.assertEqual([r['bin_index'] for r in rows], ['0','1','2'])
        for row in rows:
            self.assertEqual((row['sample_count'],row['exceedance_count'],row['sample_fraction']), ('5','2','0.4'))
            self.assertEqual(row['artifact_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(row['rf_time_occupancy'],'unknown')
            self.assertEqual(row['started_at_utc'],'2026-11-08T00:00:01.000001Z')
            self.assertEqual(row['ended_at_utc'],'2026-11-08T00:00:05.000002Z')
            self.assertEqual(json.loads(row['source_ids_json']),[f'sweep-{i}' for i in range(1,6)])
        self.assertEqual(parse_artifact(raw)['items'][0]['settings']['metadata']['counter'],9007199254740993)

    def test_duplicate_frequencies_are_distinct_bins(self):
        artifact=fixture(); artifact['data']['frequencies_hz']=[100,100,100]
        rows=list(csv.DictReader(io.StringIO(export_csv(encode(artifact)))))
        self.assertEqual(len(rows),3)
        self.assertEqual([r['bin_index'] for r in rows],['0','1','2'])

    def test_spreadsheet_text_is_json_encoded(self):
        artifact=fixture(); artifact['data']['session_id']='=HYPERLINK("bad")\n,данные'
        row=next(csv.DictReader(io.StringIO(export_csv(encode(artifact)))))
        self.assertTrue(row['session_id_json'].startswith('"'))
        self.assertEqual(json.loads(row['session_id_json']),artifact['data']['session_id'])

    def test_missing_neighbours_do_not_change_denominator(self):
        artifact=fixture(); data=artifact['data']
        data.update(requested={'before':3,'after':3},missing={'before':1,'after':1},complete=False)
        row=next(csv.DictReader(io.StringIO(export_csv(encode(artifact)))))
        self.assertEqual((row['sample_count'],row['missing_before'],row['missing_after'],row['complete']),('5','1','1','false'))

    def test_rejects_tampered_summary_and_originals(self):
        changes=[lambda a:a.update(format_version=True),lambda a:a.update(format_version=2),
            lambda a:a['data']['occupancy'].update(exceedance_counts=[3]*3,sample_fractions=[.6]*3),
            lambda a:a['data']['occupancy'].update(acquisition_gap_seconds=0),
            lambda a:a['data']['occupancy'].update(exceedance_counts=[2.0]*3),
            lambda a:a['data'].update(points=3.0),
            lambda a:a['data']['items'][0].update(levels=[0]*3),
            lambda a:a['data']['items'].reverse(),lambda a:a['data']['items'][0].update(id='sweep-3'),
            lambda a:a['data'].update(complete=False),lambda a:a['data'].update(frequencies_hz=[100.,101,102]),
            lambda a:a['data']['items'][0]['settings'].update(rbw_hz=2000)]
        for change in changes:
            artifact=fixture();change(artifact)
            with self.subTest(change=change),self.assertRaises(ValueError):parse_artifact(encode(artifact))

    def test_strict_and_bounded_input(self):
        raw=encode(fixture())
        cases=[b' '* (MAX_ARTIFACT_BYTES+1), b'[]', b'{}', b'\xff',
            raw.replace(b'"format_version": 1',b'"format_version": 1, "format_version": 1'),
            raw.replace(b'9007199254740993',b'1e999'),raw.replace(b'9007199254740993',b'NaN')]
        for value in cases:
            with self.subTest(value=value[:30]),self.assertRaises(ValueError):parse_artifact(value)

    def test_output_bound_counts_utf8_and_repeated_provenance(self):
        artifact=fixture();artifact['data']['session_id']='эфир'*200
        with patch('radioinspector.occupancy_export.MAX_CSV_BYTES', 4096):
            with self.assertRaisesRegex(ValueError,'CSV превышает'):
                export_csv(encode(artifact))

    def test_cli_never_clobbers_and_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);src=root/'source.json';dst=root/'result.csv'
            src.write_bytes(encode(fixture()))
            self.assertEqual(main([str(src),'--output',str(dst)]),0)
            expected=dst.read_bytes()
            with patch('sys.stderr',new=io.StringIO()):
                self.assertEqual(main([str(src),'--output',str(dst)]),2)
                src.write_text('{}')
                self.assertEqual(main([str(src),'--output',str(root/'bad.csv')]),2)
            self.assertEqual(dst.read_bytes(),expected)
            self.assertEqual(set(p.name for p in root.iterdir()),{'source.json','result.csv'})
