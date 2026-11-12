"""Source integrity precedes publication, including early failure paths."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ori_research import worker
from ori_research.protocol import WorkerError, encode, parse
from ori_research.resources import Budget

import test_worker as legacy
from test_derived_source import seal, source_records


class DerivedSinglePassTests(unittest.TestCase):
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

    def request(self, **kwargs):
        return dict(version=1,request_id='single-pass-fixture',operation='export',
            snapshot_path=str(self.derived),cache_directory=str(self.cache),filters={},**kwargs)

    def source_bytes(self):
        return sum(path.stat().st_size for path in self.derived.parent.iterdir())

    def no_publication(self):
        self.assertFalse(list(self.cache.glob('cache-*')))
        self.assertFalse(list(self.cache.glob('.stage-*')))

    def stale_tail(self):
        raw = self.derived.read_bytes()
        old = b'"level":137.0'
        self.assertIn(old,raw)
        self.derived.write_bytes(raw.replace(old,b'"level":138.0',1))
        self.assertEqual(self.derived.stat().st_size,len(raw))

    def test_cold_source_bytes_are_read_once(self):
        expected = self.source_bytes()
        response = self.success(self.request())
        self.assertFalse(response['result']['cache_hit'])
        self.assertEqual(response['metrics']['source_bytes_read'],expected)

    def test_cache_hit_source_bytes_are_read_once(self):
        first = self.success(self.request())
        response = self.success(self.request())
        self.assertTrue(response['result']['cache_hit'])
        self.assertEqual(response['result']['manifest_sha256'],first['result']['manifest_sha256'])
        self.assertEqual(response['metrics']['source_bytes_read'],self.source_bytes())

    def test_cold_valid_envelope_with_stale_tail_digest_cannot_publish(self):
        self.stale_tail()
        self.failure(self.request(),code='snapshot_invalid')
        self.no_publication()

    def test_cache_hit_stale_tail_digest_cannot_return_or_change_cached_files(self):
        first = self.success(self.request())
        directory = Path(first['result']['cache_path'])
        before = {path.name:legacy.digest(path) for path in directory.iterdir()}
        self.stale_tail()
        request = self.request()
        request['operation'] = 'summary'
        self.failure(request,code='snapshot_invalid')
        self.assertEqual(before,{path.name:legacy.digest(path) for path in directory.iterdir()})

    def test_stale_cache_hit_is_rejected_before_the_actual_query_runs(self):
        self.success(self.request())
        self.stale_tail()
        request = self.request()
        request['operation'] = 'summary'
        request = parse(encode(request))
        marker = self.root/'query-entered'
        original = worker.query
        def query(*args):
            marker.touch()
            return original(*args)
        with patch.object(worker,'query',query),self.assertRaises(WorkerError) as failure:
            worker.run(request,Budget(request['limits']))
        self.assertEqual(failure.exception.code,'snapshot_invalid')
        self.assertFalse(marker.exists())

    def test_early_field_error_keeps_original_code_and_charged_digest(self):
        records = copy.deepcopy(self.records)
        next(record['value'] for record in records if record['kind']=='sweep')['levels'] = 'invalid'
        self.derived = seal(self.derived.parent,records)
        prefix = sum(len(raw) for raw in self.derived.read_bytes().splitlines(keepends=True)[:3])
        response = self.failure(self.request(),code='source_record_invalid')
        self.assertEqual(response['metrics']['source_bytes_read'],self.source_bytes()+prefix)
        self.no_publication()

    def test_stale_digest_precedes_early_parse_or_record_budget_error(self):
        for malformed in (False,True):
            with self.subTest(malformed=malformed):
                self.derived = seal(self.derived.parent,self.records)
                if malformed:
                    raw = self.derived.read_bytes()
                    self.derived.write_bytes(raw.replace(b'"kind":"stream"',b'"kind":"broken"',1))
                    request = self.request()
                else:
                    self.stale_tail()
                    request = self.request(limits={'max_record_bytes':128})
                response = self.failure(request,code='snapshot_invalid')
                self.assertGreaterEqual(response['metrics']['source_bytes_read'],self.source_bytes())
                self.no_publication()

    def test_early_quota_failure_still_validates_the_whole_source_digest(self):
        records = copy.deepcopy(self.records)
        sweep = next(record['value'] for record in records if record['kind']=='sweep')
        sweep.update(frequencies_hz=list(range(65_536)),levels=[-100.125]*65_536)
        for stale in (False,True):
            with self.subTest(stale=stale):
                self.derived = seal(self.derived.parent,records)
                if stale:
                    self.stale_tail()
                response = self.failure(self.request(limits={'cache_bytes':4096}),
                    code='snapshot_invalid' if stale else 'cache_quota_exceeded')
                self.assertGreaterEqual(response['metrics']['source_bytes_read'],self.source_bytes())
                self.no_publication()

    def test_late_count_failure_cannot_publish(self):
        metadata = self.derived.parent/'manifest.json'
        manifest = json.loads(metadata.read_text())
        manifest['counts']['observations'] += 1
        metadata.write_text(json.dumps(manifest))
        self.failure(self.request(),code='snapshot_invalid')
        self.no_publication()

    def test_source_and_metadata_identity_and_sidecars_are_checked_after_build(self):
        original = worker.build_cache
        for mutation in ('source','metadata','sidecar'):
            with self.subTest(mutation=mutation):
                self.derived = seal(self.root/('derived-'+mutation),self.records)
                self.cache = self.root/('cache-'+mutation)
                def build(*args):
                    result = original(*args)
                    if mutation=='sidecar':
                        Path(str(self.derived)+'-wal').touch()
                    else:
                        path = self.derived if mutation=='source' else self.derived.parent/'manifest.json'
                        path.write_bytes(path.read_bytes()+b' ')
                    return result
                request = parse(encode(self.request()))
                budget = Budget(request['limits'])
                with patch.object(worker,'build_cache',build),self.assertRaises(WorkerError) as failure:
                    worker.run(request,budget)
                self.assertEqual(failure.exception.code,
                    'unsealed_snapshot' if mutation=='sidecar' else 'snapshot_invalid')
                self.no_publication()


if __name__=='__main__':
    unittest.main()
