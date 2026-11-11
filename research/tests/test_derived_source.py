"""Explicit derived sources have the same science as frozen full archives."""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

import pyarrow.parquet as pq

import test_worker as legacy


def source_records(snapshot):
    with closing(sqlite3.connect(snapshot)) as db:
        db.row_factory = sqlite3.Row
        records = []
        for row in db.execute('SELECT * FROM streams ORDER BY id'):
            value = {key:row[key] for key in ('id','session_id','device','role','level_unit','label')}
            value.update(settings=json.loads(row['settings_json']),capabilities=json.loads(row['capabilities_json']))
            records.append(dict(kind='stream',value=value))
        for row in db.execute('SELECT * FROM sweeps ORDER BY id'):
            value = {key:row[key] for key in ('id','session_id','stream_id','level_unit')}
            value.update(interval=dict(started_at=row['started_at'],ended_at=row['ended_at']),
                frequencies_hz=json.loads(row['frequencies_json']),levels=json.loads(row['levels_json']),
                settings=json.loads(row['settings_json']))
            records.append(dict(kind='sweep',value=value))
        for row in db.execute('SELECT * FROM observations ORDER BY id'):
            value = {key:row[key] for key in ('id','session_id','stream_id','frequency_hz','mode','level',
                                            'level_unit','decoded_mode','audio_uri')}
            value.update(interval=dict(started_at=row['started_at'],ended_at=row['ended_at']),
                         squelch_open=None if row['squelch_open'] is None else bool(row['squelch_open']),
                         metadata=json.loads(row['metadata_json']))
            records.append(dict(kind='observation',value=value))
        return records


def seal(directory, records, *, filters=None, watermark=73):
    directory.mkdir(exist_ok=True)
    path = directory/'records.ndjson'
    raw = b''.join((json.dumps(record,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n').encode()
                   for record in records)
    path.write_bytes(raw)
    counts = {name:sum(row['kind']==kind for row in records)
              for name,kind in (('streams','stream'),('sweeps','sweep'),('observations','observation'))}
    manifest = dict(derived_source_version=1,purpose='derived-history',
        records=dict(file=path.name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()),
        event_watermark=watermark,filters=filters or {},counts=counts,audio_payloads_copied=False)
    (directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False))
    return path


class DerivedSourceTests(unittest.TestCase):
    run_worker = legacy.WorkerTests.run_worker
    success = legacy.WorkerTests.success
    failure = legacy.WorkerTests.failure

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = legacy.make_snapshot(self.root/'full')
        self.records = source_records(self.snapshot)
        self.derived = seal(self.root/'derived',self.records)
        self.cache = self.root/'cache'

    def request(self, operation='export', **kwargs):
        return dict(version=1,request_id='derived-fixture-α',operation=operation,
                    snapshot_path=str(self.derived),cache_directory=str(self.cache),filters={},**kwargs)

    def full_request(self, operation='export', **kwargs):
        request = self.request(operation,**kwargs)
        request.update(snapshot_path=str(self.snapshot),cache_directory=str(self.root/'full-cache'))
        return request

    def test_derived_export_preserves_raw_values_and_explicit_source_provenance(self):
        before = {p.name:legacy.digest(p) for p in self.derived.parent.iterdir()}
        full = self.success(self.full_request())['result']
        derived = self.success(self.request())['result']
        source = derived['manifest']['source']
        self.assertEqual(source['kind'],'scoped-derived-v1')
        self.assertTrue(source['derived'])
        self.assertNotIn('recovery_manifest_sha256',source)
        self.assertEqual(source['event_watermark'],73)
        self.assertEqual(source['sha256'],before['records.ndjson'])
        self.assertNotEqual(derived['cache_key'],full['cache_key'])
        for table in ('streams','sweeps','sweep_points','observations'):
            expected = pq.read_table(Path(full['cache_path'])/(table+'.parquet')).to_pylist()
            actual = pq.read_table(Path(derived['cache_path'])/(table+'.parquet')).to_pylist()
            for rows in (expected,actual):
                for row in rows:
                    for key in ('settings_json','capabilities_json','metadata_json'):
                        if key in row:
                            row[key] = json.loads(row[key])
            self.assertEqual(actual,expected)
        points = pq.read_table(Path(derived['cache_path'])/'sweep_points.parquet').to_pylist()
        self.assertEqual([struct.pack('>d',r['level']).hex() for r in points],
                         ['c059080000000000','8000000000000000','bff0000000000001','c040600000000000'])
        self.assertEqual(before,{p.name:legacy.digest(p) for p in self.derived.parent.iterdir()})
        self.assertTrue(self.success(self.request())['result']['cache_hit'])

    def test_summary_and_occupancy_match_full_frozen_source(self):
        for operation,extra in (('summary',{}),('occupancy',dict(threshold=dict(value=137,unit='s_meter_count')))):
            with self.subTest(operation=operation):
                full = self.success(self.full_request(operation,**extra))['result']
                derived = self.success(self.request(operation,**extra))['result']
                self.assertEqual(derived['rows'],full['rows'])
                self.assertEqual(derived['event_watermark'],full['event_watermark'])

    def test_json_keys_and_values_keep_distinct_unicode_and_original_bits(self):
        value = next(row['value'] for row in self.records if row['kind']=='sweep')
        value['settings'] = {'é':-0.0,'é':-1.0000000000000002,'large':2**80,
                             '$serde_json::private::Number':{'source':'intact'}}
        self.derived = seal(self.derived.parent,self.records)
        result = self.success(self.request())['result']
        raw = pq.read_table(Path(result['cache_path'])/'sweeps.parquet').to_pylist()[0]['settings_json']
        settings = json.loads(raw)
        self.assertEqual(set(settings),set(value['settings']))
        self.assertEqual(struct.pack('>d',settings['é']).hex(),'8000000000000000')
        self.assertEqual(struct.pack('>d',settings['é']).hex(),'bff0000000000001')
        self.assertEqual(settings['large'],2**80)

    def test_digest_scope_and_linked_source_are_rejected_before_publication(self):
        self.derived.write_bytes(self.derived.read_bytes()+b' ')
        self.failure(self.request(),code='snapshot_invalid')
        self.derived = seal(self.derived.parent,self.records,filters={'session_id':'selected'})
        self.failure(self.request(),code='invalid_filter')
        if hasattr(Path,'symlink_to'):
            outside = self.root/'unrelated'
            outside.mkdir()
            target = outside/'outside.ndjson'
            self.derived.rename(target)
            self.derived.symlink_to(target)
            self.failure(self.request(),code='snapshot_invalid')
        self.assertFalse(list(self.cache.glob('cache-*')))

    def test_records_cannot_widen_the_declared_session_or_stream_scope(self):
        filters = {'session_id':legacy.FIXTURE['session']['id']}
        records = copy.deepcopy(self.records)
        records[0]['value']['session_id'] = 'foreign-session'
        self.derived = seal(self.derived.parent,records,filters=filters)
        request = self.request()
        request['filters'] = filters
        self.failure(request,code='source_record_invalid')
        self.assertFalse(list(self.cache.glob('cache-*')))

    def test_record_budget_and_malformed_rows_cannot_publish_partial_tables(self):
        self.failure(self.request(limits={'max_record_bytes':128}),code='source_record_too_large')
        for mutation in ('duplicate','unknown','counts'):
            with self.subTest(mutation=mutation):
                records = copy.deepcopy(self.records)
                if mutation=='duplicate':
                    records.insert(1,copy.deepcopy(records[0]))
                elif mutation=='unknown':
                    records[0]['kind']='session'
                self.derived = seal(self.derived.parent,records)
                if mutation=='counts':
                    path = self.derived.parent/'manifest.json'
                    manifest = json.loads(path.read_text())
                    manifest['counts']['sweeps'] += 1
                    path.write_text(json.dumps(manifest))
                self.failure(self.request(),code='snapshot_invalid' if mutation=='counts' else 'source_record_invalid')
                self.assertFalse(list(self.cache.glob('cache-*')))
                self.assertFalse(list(self.cache.glob('.stage-*')))

    def test_declared_selection_keeps_full_source_point_indexes(self):
        filters = {'min_frequency_hz':100000002,'max_frequency_hz':100000003}
        records = [row for row in self.records if row['kind']!='observation']
        self.derived = seal(self.derived.parent,records,filters=filters)
        request = self.request()
        request['filters'] = filters
        result = self.success(request)['result']
        points = pq.read_table(Path(result['cache_path'])/'sweep_points.parquet').to_pylist()
        self.assertEqual([r['point_index'] for r in points],[1,2])
        sweep = pq.read_table(Path(result['cache_path'])/'sweeps.parquet').to_pylist()[0]
        self.assertEqual((sweep['source_point_count'],sweep['exported_point_count']),(4,2))


if __name__=='__main__':
    unittest.main()
