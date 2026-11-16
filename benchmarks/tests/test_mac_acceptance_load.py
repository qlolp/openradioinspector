from pathlib import Path
from contextlib import closing
import tempfile
import unittest
import sqlite3
import os
from types import SimpleNamespace
from unittest.mock import Mock,patch
from benchmarks.ori_bench.config import Config,BenchmarkError
from benchmarks.ori_bench.runner import run
from benchmarks.tests.test_benchmarks import http_fixture


class NativeLoadReadinessTests(unittest.TestCase):
    def test_unpublished_ui_process_status_waits_until_observed_exit(self):
        from benchmarks.ori_bench.mac_acceptance import load
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with http_fixture(root/'source.sqlite',audio=False) as (peer,url):
                # Only the Mac bundle, UI process and power assertion are external
                # stand-ins. Warmup still writes through the real HTTP client.
                node=SimpleNamespace(start=lambda:url,close=lambda **_kwargs:None,records=[],
                    owner=SimpleNamespace(status=lambda:{'phase':'running','child_pid':os.getpid()}))
                owner=SimpleNamespace(status=Mock(side_effect=[None,dict(phase='exited',child_pid=123,
                    child_start='fixture',exit_code=1,cleanup_failed=False,control_serial=0)]))
                window=SimpleNamespace(owner=owner,start=lambda:None,stop=lambda *args,**kwargs:None)
                awake=SimpleNamespace(poll=lambda:0,wait=lambda **kwargs:0)
                identity=dict(app=str(root/'RC.app'),source_revision='controlled-ui-fixture',
                    core_sha256='a'*64,native_sha256='b'*64,supervisor_sha256='c'*64,manifest_sha256='d'*64)
                with patch.object(load,'host',return_value=dict(system='Darwin',architecture='arm64',macos_version='27.3',memory_bytes=16*1024**3)),\
                     patch.object(load,'bundle_identity',return_value=identity),\
                     patch.object(load.subprocess,'run'),patch.object(load.subprocess,'Popen',return_value=awake),\
                     patch.object(load,'PackageNode',return_value=node),patch.object(load,'NativeWindow',return_value=window):
                    report,status=load.run_load(root/'RC.app',root/'manifest.json',root/'result',smoke_seconds=1)
                self.assertEqual(status,1)
                self.assertEqual(report['errors'][0]['code'],'native_unavailable',report['errors'])
                self.assertFalse(report['point2_passed'])
                with peer.lock:
                    self.assertEqual(peer.db.execute('SELECT count(*) FROM measurements').fetchone()[0],2)
                self.assertFalse((root/'result/producer/offers.sqlite').exists())

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
