"""Real SQLite -> Parquet -> DuckDB contracts, independent of the core runtime."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/measurement-v1.json').read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_snapshot(directory, *, schema=6, copies=1):
    """A sealed fixture with the canonical archive column names and real audio."""
    directory.mkdir()
    path = directory / 'archive.sqlite'
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE sessions (id TEXT PRIMARY KEY);
        CREATE TABLE streams (id TEXT PRIMARY KEY,session_id TEXT,device TEXT,role TEXT,
            level_unit TEXT,label TEXT,settings_json TEXT,capabilities_json TEXT);
        CREATE TABLE sweeps (id TEXT PRIMARY KEY,session_id TEXT,stream_id TEXT,
            started_at TEXT,ended_at TEXT,frequencies_json TEXT,levels_json TEXT,
            level_unit TEXT,settings_json TEXT);
        CREATE TABLE observations (id TEXT PRIMARY KEY,session_id TEXT,stream_id TEXT,
            started_at TEXT,ended_at TEXT,frequency_hz INTEGER,mode TEXT,level REAL,
            level_unit TEXT,squelch_open INTEGER,decoded_mode TEXT,audio_uri TEXT,metadata_json TEXT);
        CREATE TABLE events (sequence INTEGER PRIMARY KEY);
        CREATE TABLE instrument_artifacts (id TEXT PRIMARY KEY,payload BLOB);
    ''')
    db.execute(f'PRAGMA user_version={schema}')
    db.execute('INSERT INTO sessions VALUES (?)', (FIXTURE['session']['id'],))
    for stream in FIXTURE['session']['streams']:
        db.execute('INSERT INTO streams VALUES (?,?,?,?,?,?,?,?)', (
            stream['id'], FIXTURE['session']['id'], stream['device'], stream['role'],
            stream['level_unit'], 'Synthetic', '{"private":"synthetic-private"}', '{}'))
    sweep = FIXTURE['sweep']
    for n in range(copies):
        db.execute('INSERT INTO sweeps VALUES (?,?,?,?,?,?,?,?,?)', (
            sweep['id'] if n == 0 else f'sweep-{n:08d}', sweep['session_id'], sweep['stream_id'],
            sweep['interval']['started_at'], sweep['interval']['ended_at'],
            json.dumps(sweep['frequencies_hz']), json.dumps(sweep['levels']),
            sweep['level_unit'], json.dumps(sweep['settings'], ensure_ascii=False)))
    obs = FIXTURE['observation']
    db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
        obs['id'], obs['session_id'], obs['stream_id'], obs['interval']['started_at'],
        obs['interval']['ended_at'], obs['frequency_hz'], obs['mode'], obs['level'],
        obs['level_unit'], 1, None, 'artifact:original-audio', json.dumps(obs['metadata'])))
    db.execute('INSERT INTO events VALUES (73)')
    db.execute('INSERT INTO instrument_artifacts VALUES (?,?)', ('original-audio', b'PCM_PRIVATE_AUDIO'))
    db.commit()
    db.close()
    artifacts = directory / 'artifacts.ndjson'
    artifacts.write_text('{"id":"original-audio"}\n')
    manifest = {'backup_version': 1, 'purpose': 'private-recovery', 'archive_schema': schema,
        'archive': {'file': 'archive.sqlite', 'sha256': digest(path), 'bytes': path.stat().st_size},
        'artifacts': {'file': 'artifacts.ndjson', 'sha256': digest(artifacts)},
        'counts': {'sweeps': copies, 'observations': 1, 'events': 1, 'artifacts': 1},
        'event_watermark': 73}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    return path


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = make_snapshot(self.root / 'snapshot')
        self.cache = self.root / 'cache'

    def request(self, operation='export', **kwargs):
        return dict(version=1, request_id='fixture-request-α', operation=operation,
                    snapshot_path=str(self.snapshot), cache_directory=str(self.cache),
                    filters={}, **kwargs)

    def run_worker(self, request=None, *, raw=None):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), OPENBLAS_NUM_THREADS='1')
        process = subprocess.run([sys.executable, '-m', 'ori_research'],
            input=raw if raw is not None else json.dumps(request), text=True,
            capture_output=True, env=env, timeout=30)
        self.assertNotIn('synthetic-private', process.stdout + process.stderr)
        self.assertNotIn('PCM_PRIVATE_AUDIO', process.stdout + process.stderr)
        return process

    def success(self, request):
        process = self.run_worker(request)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        response = json.loads(process.stdout)
        self.assertTrue(response['ok'])
        self.assertEqual(response['request_id'], request['request_id'])
        self.assertEqual(response['version'], 1)
        return response

    def failure(self, request=None, *, code=None, raw=None):
        process = self.run_worker(request, raw=raw)
        self.assertNotEqual(process.returncode, 0)
        response = json.loads(process.stdout)
        self.assertFalse(response['ok'])
        if code:
            self.assertEqual(response['error']['code'], code, response)
        return response

    def test_export_preserves_bits_integer_hz_utc_settings_and_source_ids(self):
        before = {p.name: digest(p) for p in self.snapshot.parent.iterdir()}
        response = self.success(self.request())
        result = response['result']
        cache = Path(result['cache_path'])
        points = pq.read_table(cache / 'sweep_points.parquet').to_pylist()
        self.assertEqual([r['frequency_hz'] for r in points], [100000001,100000002,100000003,100000004])
        self.assertEqual([struct.pack('>d', r['level']).hex() for r in points],
                         ['c059080000000000','8000000000000000','bff0000000000001','c040600000000000'])
        self.assertEqual([r['point_index'] for r in points], [0,1,2,3])
        self.assertTrue(all(r['sweep_id'] == 'fixture-sweep-α' for r in points))
        self.assertTrue(all(r['started_at_us'] == 1794398400123456 for r in points))
        self.assertTrue(all(r['ended_at_us'] == 1794398400223456 for r in points))
        sweeps = pq.read_table(cache / 'sweeps.parquet').to_pylist()
        self.assertEqual(json.loads(sweeps[0]['settings_json']), FIXTURE['sweep']['settings'])
        observations = pq.read_table(cache / 'observations.parquet').to_pylist()
        self.assertEqual(observations[0]['level_unit'], 's_meter_count')
        self.assertEqual(observations[0]['id'], 'fixture-observation-α')
        self.assertEqual(observations[0]['audio_uri'], 'artifact:original-audio')
        self.assertEqual(result['manifest']['source']['event_watermark'], 73)
        self.assertEqual(result['manifest']['source']['sha256'], before['archive.sqlite'])
        for path in cache.iterdir():
            self.assertNotIn(b'PCM_PRIVATE_AUDIO', path.read_bytes())
        self.assertEqual(before, {p.name: digest(p) for p in self.snapshot.parent.iterdir()})
        again = self.success(self.request())
        self.assertTrue(again['result']['cache_hit'])
        self.assertEqual(again['result']['manifest_sha256'], result['manifest_sha256'])

    def test_unknown_schema_is_rejected_before_export(self):
        self.snapshot = make_snapshot(self.root / 'future', schema=7)
        self.failure(self.request(), code='unsupported_schema')

    def test_unsealed_snapshot_or_digest_mismatch_is_rejected(self):
        sidecar = Path(str(self.snapshot) + '-wal')
        sidecar.write_bytes(b'')
        self.failure(self.request(), code='unsealed_snapshot')
        sidecar.unlink()
        with self.snapshot.open('ab') as stream:
            stream.write(b'damaged')
        self.failure(self.request(), code='snapshot_invalid')

    def test_manifest_watermark_must_match_the_sealed_source(self):
        path = self.snapshot.parent / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['event_watermark'] = 74
        path.write_text(json.dumps(manifest))
        self.failure(self.request(), code='snapshot_invalid')

    def test_invalid_protocol_and_filters_are_structured_errors(self):
        for change in ({'version':2}, {'operation':'sql'}, {'sql':'DROP TABLE sweeps'},
                       {'snapshot_path':'relative.sqlite'}, {'page_size':0},
                       {'filters':{'min_frequency_hz':True}},
                       {'filters':{'min_frequency_hz':2,'max_frequency_hz':1}},
                       {'filters':{'started_at_us':2,'ended_at_us':1}},
                       {'filters':{'stream_id':[]}}, {'filters':{'sql':'anything'}},
                       {'limits':{'duckdb_threads':3}}, {'limits':{'cache_bytes':0}}):
            with self.subTest(change=change):
                request = self.request()
                request.update(change)
                self.failure(request, code='invalid_request')
        for raw in ('{}\n{}', '{', '[]', '{"version":NaN}', ' ' * 65537):
            with self.subTest(raw=raw[:20]):
                self.failure(raw=raw, code='invalid_request')

    def test_summary_pages_are_typed_and_bound_to_query(self):
        first = self.success(self.request('summary', page_size=1))['result']
        self.assertEqual(len(first['rows']),1)
        self.assertIsNotNone(first['next_cursor'])
        row = first['rows'][0]
        self.assertEqual((row['kind'],row['measurement_count'],row['sample_count']),('observation',1,1))
        second = self.success(self.request('summary',page_size=1,cursor=first['next_cursor']))['result']
        self.assertEqual(second['rows'][0]['kind'],'sweep')
        self.assertEqual(second['rows'][0]['measurement_count'],1)
        self.assertEqual(second['rows'][0]['sample_count'],4)
        self.assertIsNone(second['next_cursor'])
        wrong = self.request('summary',page_size=1,cursor=first['next_cursor'])
        wrong['filters']={'min_frequency_hz':100000003}
        self.failure(wrong,code='invalid_cursor')

    def test_occupancy_is_sample_fraction_with_original_units_and_frequency_filters(self):
        request = self.request('occupancy',threshold={'value':-10.0,'unit':'dBm'},page_size=2)
        result = self.success(request)['result']
        self.assertEqual([(r['frequency_hz'],r['occupied_count'],r['sample_count']) for r in result['rows']],
                         [(100000001,0,1),(100000002,1,1)])
        self.assertEqual([r['occupancy_ratio'] for r in result['rows']],[0.0,1.0])
        request['cursor']=result['next_cursor']
        last = self.success(request)['result']
        self.assertEqual([r['frequency_hz'] for r in last['rows']],[100000003,100000004])
        self.assertIsNone(last['next_cursor'])
        request = self.request('occupancy',threshold={'value':100,'unit':'s_meter_count'})
        result = self.success(request)['result']
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['rows'][0]['occupied_count'],1)
        self.assertEqual(result['rows'][0]['kind'],'observation')
        self.failure(self.request('occupancy'),code='invalid_request')

    def test_filtered_exports_bind_cache_and_preserve_original_point_indexes(self):
        request = self.request()
        request['filters']={'session_id':'fixture-сессия','stream_id':'spectrum/one',
            'started_at_us':1794398400123456,'ended_at_us':1794398400123457,
            'min_frequency_hz':100000002,'max_frequency_hz':100000003}
        result = self.success(request)['result']
        cache = Path(result['cache_path'])
        points = pq.read_table(cache/'sweep_points.parquet').to_pylist()
        self.assertEqual([r['point_index'] for r in points],[1,2])
        sweep = pq.read_table(cache/'sweeps.parquet').to_pylist()[0]
        self.assertEqual((sweep['source_point_count'],sweep['exported_point_count']),(4,2))
        self.assertEqual(pq.read_table(cache/'observations.parquet').num_rows,0)
        other = self.success(self.request())['result']
        self.assertNotEqual(result['cache_key'],other['cache_key'])

    def test_cache_quota_cannot_publish_partial_export(self):
        self.failure(self.request(limits={'cache_bytes':4096}),code='cache_quota_exceeded')
        self.assertEqual(list(self.cache.glob('cache-*/manifest.json')),[])
        self.assertLessEqual(sum(p.stat().st_size for p in self.cache.rglob('*') if p.is_file()),4096)
        self.success(self.request())

    def test_corrupt_owned_cache_is_rebuilt_from_original_source(self):
        result = self.success(self.request())['result']
        cache = Path(result['cache_path'])
        (cache/'sweep_points.parquet').write_bytes(b'corrupt')
        rebuilt = self.success(self.request())['result']
        self.assertFalse(rebuilt['cache_hit'])
        self.assertTrue(rebuilt['cache_rebuilt'])
        self.assertEqual(pq.read_table(cache/'sweep_points.parquet').num_rows,4)

    def test_unowned_cache_and_snapshot_overlap_are_rejected(self):
        self.cache.mkdir()
        precious = self.cache/'user-data'
        precious.write_bytes(b'keep')
        self.failure(self.request(),code='unsafe_cache')
        self.assertEqual(precious.read_bytes(),b'keep')
        request=self.request()
        request['cache_directory']=str(self.snapshot.parent/'cache')
        self.failure(request,code='unsafe_cache')

    def test_bounded_batches_and_limits_report_actual_measurements(self):
        self.snapshot = make_snapshot(self.root/'many',copies=257)
        response = self.success(self.request(limits={'batch_rows':3}))
        self.assertEqual(response['result']['manifest']['counts']['sweep_points'],1028)
        self.assertLessEqual(response['metrics']['max_batch_rows'],3)
        self.assertLessEqual(response['metrics']['max_batch_bytes'],8*1024*1024)
        self.assertGreater(response['metrics']['source_bytes_read'],0)
        self.assertGreater(response['metrics']['peak_rss_bytes'],0)

    def test_cancelled_build_has_no_valid_partial_cache_and_can_restart(self):
        self.snapshot = make_snapshot(self.root/'cancel',copies=80)
        request = self.request(limits={'batch_rows':1,'io_bytes_per_second':65536})
        env = dict(os.environ,PYTHONPATH=str(ROOT/'src'),OPENBLAS_NUM_THREADS='1')
        child = subprocess.Popen([sys.executable,'-m','ori_research'],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        child.stdin.write(json.dumps(request))
        child.stdin.close()
        child.stdin = None
        deadline = time.monotonic()+15
        while not list(self.cache.glob('.stage-*')) and time.monotonic()<deadline and child.poll() is None:
            time.sleep(0.01)
        self.assertIsNone(child.poll(),'Worker exited before cancellation could be exercised')
        self.assertTrue(list(self.cache.glob('.stage-*')))
        self.failure(self.request(),code='cache_busy')
        child.terminate()
        child.communicate(timeout=10)
        self.assertNotEqual(child.returncode,0)
        self.assertEqual(list(self.cache.glob('cache-*/manifest.json')),[])
        result = self.success(self.request())['result']
        self.assertEqual(result['manifest']['counts']['sweeps'],80)
        self.assertEqual(list(self.cache.glob('.stage-*')),[])

    def test_unsafe_links_duplicate_keys_and_nonfinite_values_are_rejected(self):
        self.failure(raw='{"version":1,"version":1}',code='invalid_request')
        request=self.request('occupancy',threshold={'value':float('inf'),'unit':'dBm'})
        self.failure(request,code='invalid_request')
        linked = self.root/'linked.sqlite'
        try:
            linked.symlink_to(self.snapshot)
        except OSError:
            self.skipTest('Symlink creation is unavailable')
        request=self.request()
        request['snapshot_path']=str(linked)
        self.failure(request,code='snapshot_invalid')

    def test_missing_scope_and_null_levels_do_not_make_occupancy_samples(self):
        request=self.request()
        request['filters']={'stream_id':'absent'}
        self.failure(request,code='invalid_filter')
        db=sqlite3.connect(self.snapshot)
        db.execute('UPDATE observations SET level=NULL')
        db.commit()
        db.close()
        self.reseal()
        result=self.success(self.request('occupancy',threshold={'value':100,'unit':'s_meter_count'}))['result']
        self.assertEqual(result['rows'],[])
        result=self.success(self.request('summary'))['result']
        observation=next(row for row in result['rows'] if row['kind']=='observation')
        self.assertEqual((observation['sample_count'],observation['valid_level_count']),(1,0))
        self.assertIsNone(observation['min_level'])

    def reseal(self):
        path=self.snapshot.parent/'manifest.json'
        manifest=json.loads(path.read_text())
        manifest['archive'].update(sha256=digest(self.snapshot),bytes=self.snapshot.stat().st_size)
        path.write_text(json.dumps(manifest))

    def test_source_row_size_and_point_limits_are_enforced_before_publication(self):
        self.failure(self.request(limits={'max_points_per_sweep':3}),code='source_record_too_large')
        self.assertEqual(list(self.cache.glob('cache-*/manifest.json')),[])
        db=sqlite3.connect(self.snapshot)
        db.execute('UPDATE sweeps SET settings_json=?',(json.dumps({'large':'x'*8192}),))
        db.commit()
        db.close()
        self.reseal()
        self.failure(self.request(limits={'max_record_bytes':4096}),code='source_record_too_large')
        self.assertEqual(list(self.cache.glob('cache-*/manifest.json')),[])

    def test_integer_frequency_and_aware_source_time_are_required(self):
        db=sqlite3.connect(self.snapshot)
        db.execute('UPDATE sweeps SET frequencies_json=?',(json.dumps([100000001.0,100000002,100000003,100000004]),))
        db.commit()
        db.close()
        self.reseal()
        self.failure(self.request(),code='source_record_invalid')
        db=sqlite3.connect(self.snapshot)
        db.execute('UPDATE sweeps SET frequencies_json=?,started_at=?',
                   (json.dumps(FIXTURE['sweep']['frequencies_hz']),'2026-11-11T12:00:00.123456'))
        db.commit()
        db.close()
        self.reseal()
        self.failure(self.request(),code='source_record_invalid')

    def test_strict_rss_limit_is_cooperatively_rejected(self):
        self.failure(self.request(limits={'worker_rss_bytes':1}),code='rss_limit_exceeded')

    def test_malformed_snapshot_returns_structured_error(self):
        request=self.request()
        request['snapshot_path']=str(self.root/'missing/archive.sqlite')
        self.failure(request,code='snapshot_invalid')

    def test_sibling_snapshot_identity_change_never_reuses_old_cache(self):
        before=self.success(self.request())['result']
        db=sqlite3.connect(self.snapshot)
        db.execute('UPDATE observations SET level=138')
        db.commit()
        db.close()
        self.reseal()
        after=self.success(self.request())['result']
        self.assertNotEqual(before['cache_key'],after['cache_key'])
        self.assertFalse(after['cache_hit'])

    def test_native_query_limits_and_separate_temporary_quota_are_reported(self):
        response=self.success(self.request('summary',limits={'duckdb_threads':1,
            'duckdb_memory_bytes':128*1024**2,'temp_bytes':1024*1024}))
        self.assertEqual(response['metrics']['duckdb']['threads'],1)
        self.assertEqual(response['metrics']['duckdb']['memory_limit_bytes'],128*1024**2)
        self.assertEqual(response['metrics']['duckdb']['max_temp_bytes'],1024*1024)
        self.assertFalse(response['metrics']['duckdb']['external_access'])
        temporary=self.cache/'.temporary'
        self.assertTrue(temporary.is_dir())
        self.assertEqual(list(temporary.iterdir()),[])
        retained=temporary/'foreign-file'
        retained.write_bytes(b'keep-me')
        self.failure(self.request('summary',limits={'temp_bytes':1}),code='temp_quota_exceeded')
        self.assertEqual(retained.read_bytes(),b'keep-me')

    def test_submicrosecond_source_time_is_rejected_instead_of_truncated(self):
        db=sqlite3.connect(self.snapshot)
        db.execute('UPDATE sweeps SET started_at=?',('2026-11-11T12:00:00.1234567Z',))
        db.commit()
        db.close()
        self.reseal()
        self.failure(self.request(),code='source_record_invalid')

    def test_deeply_corrupt_cache_manifest_can_be_recreated(self):
        result=self.success(self.request())['result']
        (Path(result['cache_path'])/'manifest.json').write_text('['*1200+'0'+']'*1200)
        rebuilt=self.success(self.request())['result']
        self.assertTrue(rebuilt['cache_rebuilt'])
        self.assertEqual(pq.read_table(Path(rebuilt['cache_path'])/'sweep_points.parquet').num_rows,4)

    def precision(self,stored,bits,*,version=1):
        db=sqlite3.connect(self.snapshot)
        db.executescript('CREATE TABLE IF NOT EXISTS ori_archive_schema(singleton INTEGER PRIMARY KEY,version INTEGER);'
            'CREATE TABLE IF NOT EXISTS ori_observation_level_bits(observation_id TEXT PRIMARY KEY '
            'REFERENCES observations(id),level_bits_le BLOB);')
        db.execute('INSERT OR REPLACE INTO ori_archive_schema VALUES(1,?)',(version,))
        db.execute('UPDATE observations SET level=?',(stored,))
        db.execute('INSERT OR REPLACE INTO ori_observation_level_bits VALUES(?,?)',
                   ('fixture-observation-α',bits))
        db.commit();db.close();self.reseal()

    def test_canonical_precision_bits_preserve_observation_negative_zero_and_nextafter(self):
        for value,expected in ((-0.0,'8000000000000000'),(-1.0000000000000002,'bff0000000000001')):
            with self.subTest(value=value):
                self.precision(value,struct.pack('<d',value))
                before=digest(self.snapshot)
                result=self.success(self.request())['result']
                rows=pq.read_table(Path(result['cache_path'])/'observations.parquet').to_pylist()
                self.assertEqual(struct.pack('>d',rows[0]['level']).hex(),expected)
                self.assertEqual(result['manifest']['source']['canonical_extensions'],{'observation_level_bits':1})
                self.assertEqual(digest(self.snapshot),before)

    def test_legacy_real_without_precision_is_never_reinterpreted_or_backfilled(self):
        db=sqlite3.connect(self.snapshot);db.execute('UPDATE observations SET level=?',(-0.0,))
        db.commit();stored=db.execute('SELECT level FROM observations').fetchone()[0];db.close();self.reseal()
        self.assertEqual(struct.pack('>d',stored).hex(),'0000000000000000')
        result=self.success(self.request())['result']
        rows=pq.read_table(Path(result['cache_path'])/'observations.parquet').to_pylist()
        self.assertEqual(struct.pack('>d',rows[0]['level']).hex(),'0000000000000000')

    def test_unknown_canonical_extension_version_fails_closed_without_source_changes(self):
        self.precision(-0.0,struct.pack('<d',-0.0),version=2)
        before=digest(self.snapshot)
        self.failure(self.request(),code='unsupported_schema')
        self.assertEqual(digest(self.snapshot),before)
        self.assertEqual(list(self.cache.glob('cache-*/manifest.json')),[])

    def test_invalid_or_inconsistent_canonical_level_bits_are_not_silently_ignored(self):
        for stored,bits in ((137.0,b'short'),(137.0,'12345678'),(137.0,struct.pack('<d',138.0)),
                            (137.0,struct.pack('<d',float('inf'))),(137.0,struct.pack('<d',float('nan'))),
                            (None,struct.pack('<d',1.0))):
            with self.subTest(stored=stored,bits=bits):
                self.precision(stored,bits)
                self.failure(self.request(),code='source_record_invalid')
                self.assertEqual(list(self.cache.glob('cache-*/manifest.json')),[])

    def test_incomplete_precision_extension_is_rejected(self):
        db=sqlite3.connect(self.snapshot)
        db.execute('CREATE TABLE ori_archive_schema(singleton INTEGER PRIMARY KEY,version INTEGER)')
        db.execute('INSERT INTO ori_archive_schema VALUES(1,1)');db.commit();db.close();self.reseal()
        self.failure(self.request(),code='snapshot_invalid')

    def test_precision_extension_does_not_require_historical_supplement_rows(self):
        self.precision(137.0,struct.pack('<d',137.0))
        db=sqlite3.connect(self.snapshot);db.execute('DELETE FROM ori_observation_level_bits')
        db.commit();db.close();self.reseal()
        result=self.success(self.request())['result']
        self.assertEqual(pq.read_table(Path(result['cache_path'])/'observations.parquet')['level'].to_pylist(),[137.0])


if __name__ == '__main__':
    unittest.main()
