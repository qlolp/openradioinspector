from pathlib import Path
from contextlib import closing
import tempfile
import unittest
import sqlite3
from benchmarks.ori_bench.config import Config,BenchmarkError
from benchmarks.ori_bench.runner import run
from benchmarks.tests.test_benchmarks import http_fixture


class NativeLoadReadinessTests(unittest.TestCase):
    def test_prefix_snapshot_cannot_claim_a_complete_final_restore(self):
        from benchmarks.ori_bench.mac_acceptance.load import complete_restore
        config=Config('http://127.0.0.1:1',Path('/unused'),workload_profile='macos-rc1')
        self.assertFalse(complete_restore(config,{'status':'passed','source_counts':{'sweep':9000,'observation':9000}}))
        self.assertTrue(complete_restore(config,{'status':'passed','source_counts':{'sweep':18000,'observation':18000}}))

    def test_restore_verification_uses_independent_ledger_and_preserves_original_proof(self):
        from benchmarks.ori_bench.mac_acceptance.load import restored_ledger
        from benchmarks.ori_bench.ledger import Ledger
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);original=root/'offers.sqlite'
            source=Ledger(original)
            source.db.execute("INSERT INTO offers(id,kind,idx,verified) VALUES ('original','sweep',0,1)")
            source.db.commit();source.close()
            restored=restored_ledger(original,root/'restore-offers.sqlite')
            self.assertEqual(restored.get('original')['verified'],0)
            restored.close()
            with closing(sqlite3.connect(original)) as db:
                self.assertEqual(db.execute('SELECT verified FROM offers').fetchone()[0],1)

    def test_cleanup_before_queue_start_does_not_hide_preflight_error(self):
        from benchmarks.ori_bench.mac_acceptance.queue_probe import QueueProbe
        with tempfile.TemporaryDirectory() as d:
            probe=QueueProbe(Config('http://127.0.0.1:1',Path(d)),lambda *args:None)
            probe.close()

    def test_unready_native_observer_prevents_first_offered_source(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with http_fixture(root/'source.sqlite',audio=False) as (peer,url):
                config=Config(url,root/'result',workload_profile='macos-rc1',duration_seconds=.1,
                              points=4,history_repeats=1)
                calls=[]
                def ready():
                    calls.append('called')
                    raise BenchmarkError('native_unavailable','No actual display observer')
                report,status=run(config,before_production=ready)
                self.assertEqual(status,1)
                self.assertEqual(calls,['called'])
                self.assertIn('native_unavailable',str(report['errors']))
                self.assertFalse(any('/sweeps' in str(path) or '/observations' in str(path) for path in peer.posts))
