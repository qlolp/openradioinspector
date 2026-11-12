"""Exact point cells and bounded work, independent of scientific aggregation."""
from contextlib import ExitStack
import json
from pathlib import Path
import struct
import tempfile
import unittest

import pyarrow.parquet as pq

from ori_research.export import BatchWriter, SCHEMAS, _sweep
from ori_research.protocol import DEFAULT_LIMITS, WorkerError
from ori_research.resources import Budget


class EncodedText(str):
    calls = 0

    def encode(self, *args, **kwargs):
        type(self).calls += 1
        return super().encode(*args, **kwargs)


def row(frequencies=None, levels=None):
    return dict(id='mes-é-e\u0301-"\\', session_id='site-漢', stream_id='scan-é',
        started_at='1970-01-01T00:00:00Z', ended_at='1970-01-01T00:00:01Z',
        frequencies_json=json.dumps(frequencies or [9007199254740993,9007199254740994,2**63-2,2**63-1]),
        levels_json=json.dumps(levels or [-100.125,-0.0,-1.0000000000000002,-32.75]),
        level_unit='raw S-meter',settings_json='{"unicode":"é-é","quoted":"\\\""}')


class ColumnarPointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def export(self, source, *, filters=None, limits=None, scalar=False):
        directory = self.root/str(len(list(self.root.iterdir())))
        directory.mkdir()
        budget = Budget(DEFAULT_LIMITS | (limits or {}))
        with ExitStack() as stack:
            writers = {name:BatchWriter(name,directory/(name+'.parquet'),budget,stack) for name in SCHEMAS}
            if scalar:
                # Hand-checked scope/time cells; no projection helper from _sweep.
                frequencies = json.loads(source['frequencies_json'])
                levels = json.loads(source['levels_json'])
                for index,(hz,level) in enumerate(zip(frequencies,levels)):
                    if filters and not filters['min_frequency_hz'] <= hz <= filters['max_frequency_hz']:
                        continue
                    writers['sweep_points'].append(dict(sweep_id=source['id'],
                        session_id=source['session_id'],stream_id=source['stream_id'],
                        started_at_us=0,ended_at_us=1_000_000,point_index=index,
                        frequency_hz=hz,level=level,level_unit=source['level_unit']))
            else:
                _sweep(writers,source,filters or {},budget)
            for writer in writers.values():
                writer.flush()
        return directory,budget

    def points(self, directory):
        return pq.ParquetFile(directory/'sweep_points.parquet').read().to_pylist()

    def test_repeated_scope_text_is_not_reencoded_per_point(self):
        source = row(list(range(2049)),[-0.0]*2049)
        for field in ('id','session_id','stream_id','level_unit'):
            source[field] = EncodedText(source[field])
        EncodedText.calls = 0
        directory,_ = self.export(source)
        actual = self.points(directory)
        self.assertEqual(len(actual),2049)
        self.assertEqual(actual[-1]['point_index'],2048)
        self.assertLessEqual(EncodedText.calls,32,
            'A fixed sweep scope must not repeat UTF-8 costing for every sample')

    def test_scalar_cells_match_exact_i64_f64_unicode_and_filtered_indexes(self):
        source = row()
        filters = dict(min_frequency_hz=9007199254740994,max_frequency_hz=2**63-2)
        actual,budget = self.export(source,filters=filters)
        reference,_ = self.export(source,filters=filters,scalar=True)
        cells = self.points(actual)
        self.assertEqual(cells,self.points(reference))
        self.assertEqual([value['point_index'] for value in cells],[1,2])
        self.assertEqual([value['frequency_hz'] for value in cells],[9007199254740994,2**63-2])
        self.assertEqual([struct.pack('>d',value['level']).hex() for value in cells],
                         ['8000000000000000','bff0000000000001'])
        self.assertEqual(cells[0]['sweep_id'],'mes-é-e\u0301-"\\')
        self.assertLessEqual(budget.metrics['max_batch_rows'],1024)
        self.assertLessEqual(budget.metrics['max_batch_bytes'],4*1024**2)
        for name in ('streams','observations'):
            self.assertEqual(pq.ParquetFile(actual/(name+'.parquet')).read().to_pylist(),[])
        saved = pq.ParquetFile(actual/'sweeps.parquet').read().to_pylist()
        self.assertEqual(saved[0]['source_point_count'],4)
        self.assertEqual(saved[0]['exported_point_count'],2)
        self.assertEqual(saved[0]['settings_json'],source['settings_json'])

    def test_small_sweeps_share_bounded_row_groups(self):
        directory = self.root/'many'
        directory.mkdir()
        budget = Budget(DEFAULT_LIMITS)
        with ExitStack() as stack:
            writers = {name:BatchWriter(name,directory/(name+'.parquet'),budget,stack) for name in SCHEMAS}
            for index in range(257):
                source = row()
                source['id'] = 'small-'+str(index)
                _sweep(writers,source,{},budget)
            for writer in writers.values():
                writer.flush()
        footer = pq.ParquetFile(directory/'sweep_points.parquet').metadata
        self.assertEqual(footer.num_rows,1028)
        self.assertEqual([footer.row_group(i).num_rows for i in range(footer.num_row_groups)],
                         [1024,4])

    def test_unicode_byte_cost_boundary_is_conservative_and_oversize_rejected(self):
        source = row([1,2,3],[-0.0,-1.0000000000000002,-100.125])
        source.update(id='é',session_id='漢',stream_id='e\u0301',level_unit='"\\')
        # Four UTF-8 strings contain 2+3+3+2 bytes. Existing conservative
        # accounting adds 64 + four 8-byte headers + five 16-byte numeric cells.
        # One point therefore costs 186 bytes; two require exactly 372.
        for byte_limit, expected_rows in ((371,1),(372,2)):
            with self.subTest(byte_limit=byte_limit):
                directory,budget = self.export(source,limits={'batch_bytes':byte_limit})
                self.assertEqual(len(self.points(directory)),3)
                self.assertEqual(budget.metrics['max_batch_rows'],expected_rows)
                self.assertLessEqual(budget.metrics['max_batch_bytes'],byte_limit)
        with self.assertRaises(WorkerError) as failure:
            self.export(source,limits={'batch_bytes':185})
        self.assertEqual(failure.exception.code,'source_record_too_large')

    def test_invalid_frequency_and_nonfinite_level_validation_is_retained(self):
        for hz in (True,-1,2**63,1.0,'1'):
            with self.subTest(hz=hz),self.assertRaises(WorkerError) as failure:
                self.export(row([hz],[0.0]))
            self.assertEqual(failure.exception.code,'source_record_invalid')
        for level in (True,float('nan'),float('inf')):
            with self.subTest(level=level),self.assertRaises(WorkerError) as failure:
                self.export(row([1],[level]))
            self.assertEqual(failure.exception.code,'source_record_invalid')


if __name__ == '__main__':
    unittest.main()
