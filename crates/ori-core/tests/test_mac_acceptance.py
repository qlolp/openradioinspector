"""Actual packaged CLI/owner/queue/backup/restore, without native or hardware acceptance."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from benchmarks.ori_bench.mac_acceptance.node import PackageNode
from benchmarks.ori_bench.mac_acceptance.queue_probe import QueueProbe
from benchmarks.ori_bench.client import Client
from benchmarks.ori_bench.config import Config,BenchmarkError
from benchmarks.ori_bench.source import session,sweep,measurement_hash


class PackagedAcceptanceIntegrationTests(unittest.TestCase):

    def test_history_without_commit_trace_keeps_owned_archive_and_all_source_values(self):
        binary=Path(os.environ['ORI_CORE_TEST_BINARY']).resolve(strict=True)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();out=root/'node';out.mkdir(mode=0o700)
            node=PackageNode(binary,out,'history-without-trace',commit_diagnostics=False)
            if os.name=='nt':
                with self.assertRaises(BenchmarkError):node.start()
                return
            try:
                config=Config(node.start(),root/'unused',run_id='history-without-trace',points=290,workload_profile='macos-rc1')
                client=Client(config)
                client.json('/api/v1/sessions',method='POST',payload=session(config),status=201)
                originals=[sweep(config,i) for i in range(100)]
                client.json(client.session_path(config)+'/sweeps/batch',method='POST',payload={'items':originals},status=201)
                actual=client.json(client.session_path(config)+'/sweeps?limit=100')
                self.assertEqual([measurement_hash('sweep',v) for v in actual],
                                 [measurement_hash('sweep',v) for v in originals])
                from benchmarks.ori_bench.ledger import Ledger
                from benchmarks.ori_bench.verify import archive
                ledger=Ledger(root/'offered.sqlite')
                try:
                    ledger.offer_many(config,'sweep',range(100))
                    for value in originals:ledger.status(value['id'],'accepted',201)
                    exported=archive(client,config,ledger)
                    self.assertEqual(exported['source_counts'],dict(sweep=100,observation=0))
                    self.assertEqual(ledger.counts()['sweep']['verified'],100)
                finally:ledger.close()
                self.assertFalse((node.receipts/'core.jsonl').exists())
                with self.assertRaises(BenchmarkError):node.request_backup()
                node.close()
                self.assertTrue(node.records[-1]['joined']);self.assertTrue(node.records[-1]['graceful'])
            finally:node.close(expect_success=False)

    def test_packaged_http_framing_body_boundaries_and_no_rejected_writes(self):
        binary=Path(os.environ['ORI_CORE_TEST_BINARY']).resolve(strict=True)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();out=root/'node';out.mkdir(mode=0o700)
            node=PackageNode(binary,out,'http-fault-cli')
            if os.name=='nt':
                with self.assertRaises(BenchmarkError):node.start()
                return
            from benchmarks.ori_bench.mac_acceptance.faults import http_faults
            try:
                config=Config(node.start(),root/'unused',run_id='http-fault-cli',points=4,workload_profile='macos-rc1')
                result=http_faults(Client(config),config)
                self.assertEqual(result['status'],'passed',result)
                self.assertEqual(result['source_count'],1)
                self.assertGreaterEqual(len(result['rejections']),8)
                node.close()
            finally:node.close(expect_success=False)

    def test_final_backup_failure_preserves_closed_native_failure_evidence(self):
        import hashlib,time,subprocess
        from types import SimpleNamespace
        from unittest.mock import patch
        from benchmarks.ori_bench.mac_acceptance import load
        from benchmarks.ori_bench.mac_acceptance.evidence import private_json
        binary=Path(os.environ['ORI_CORE_TEST_BINARY']).resolve(strict=True)
        original_run,original_popen=subprocess.run,subprocess.Popen
        awake=SimpleNamespace(poll=lambda:0,wait=lambda **_kwargs:0)

        class ExistingDestinationNode(PackageNode):
            def request_final_backup(self):
                self.final_bundle.mkdir(mode=0o700)
                super().request_final_backup()

        class ClosedRenderer:
            def __init__(self,app,root,profile):
                self.root,self.profile=root,profile
                self.owner=SimpleNamespace(status=lambda:dict(phase='running'))
            def start(self):
                p=json.loads(self.profile.read_text())
                self.run_id=p['run_id']
                rows=[dict(stage='native_identity',run_id=self.run_id,component_sha256='b'*64,at_ns=time.monotonic_ns()),
                      dict(stage='native.error',run_id=self.run_id,message='fixture display unavailable',at_ns=time.monotonic_ns())]
                (self.root/'native-fixture.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
                private_json(self.root/'native-ready.json',dict(run_id=self.run_id,
                    source_id=self.run_id+'-warmup-sweep-000000000'))
                private_json(self.root/'native-stream-ready.json',dict(run_id=self.run_id,session_id=p['session_id']))
            def stop(self,run_id,**_kwargs):
                p=self.root/'native-finished-fixture.json'
                if not p.exists():private_json(p,dict(run_id=run_id,successful=True))
                self.owner=None

        def command(args,*a,**kw):
            if args[0]=='codesign':return subprocess.CompletedProcess(args,0)
            return original_run(args,*a,**kw)
        def popen(args,*a,**kw):
            return awake if args[0]=='/usr/bin/caffeinate' else original_popen(args,*a,**kw)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            identity=dict(app=str(root/'RC.app'),source_revision='f'*40,
                core_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),native_sha256='b'*64,
                supervisor_sha256='c'*64,manifest_sha256='d'*64)
            with patch.object(load,'host',return_value=dict(system='Darwin',architecture='arm64',macos_version='27.3',memory_bytes=16*1024**3)),\
                 patch.object(load,'bundle_identity',return_value=identity),\
                 patch.object(load,'NativeWindow',ClosedRenderer),\
                 patch.object(load,'PackageNode',side_effect=lambda _binary,*a,**kw:ExistingDestinationNode(binary,*a,**kw)),\
                 patch.object(subprocess,'run',side_effect=command),patch.object(subprocess,'Popen',side_effect=popen):
                report,status=load.run_load(root/'RC.app',root/'manifest.json',root/'result',smoke_seconds=1)
            self.assertEqual(status,1)
            if os.name=='nt':
                self.assertIn('unsupported_host',[error['code'] for error in report['errors']],report)
                self.assertFalse((root/'result/producer/offers.sqlite').exists())
                self.assertFalse(report['point2_passed'])
                return
            self.assertIn('backup_failed',[error['code'] for error in report['errors']],report)
            self.assertIn('native',report,'Backup failure discarded independent closed-renderer evidence')
            self.assertEqual(report['native']['status'],'failed')
            self.assertIn('fixture display unavailable',report['native']['errors'])
            self.assertFalse(report['point2_passed'])

    def test_real_owner_queue_backup_and_restore_preserve_the_original(self):
        binary=Path(os.environ['ORI_CORE_TEST_BINARY']).resolve(strict=True)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();output=root/'node';output.mkdir(mode=0o700)
            node=PackageNode(binary,output,'actual-cli')
            if os.name=='nt':
                with self.assertRaises(BenchmarkError):node.start()
                return
            try:
                config=Config(node.start(),root/'unused',run_id='actual-cli',duration_seconds=.2,points=4,
                              workload_profile='macos-rc1')
                client=Client(config)
                client.json('/api/v1/sessions',method='POST',payload=session(config),status=201)
                source=sweep(config,0)
                client.json(client.session_path(config)+'/sweeps/batch',method='POST',payload={'items':[source]},status=201)
                events=[]
                queue=QueueProbe(config,lambda stage,fields:events.append(dict(stage=stage,**fields)))
                queue.start();queue.thread.join(15)
                self.assertFalse(queue.thread.is_alive())
                self.assertEqual(queue.errors,[])
                accepted=[e for e in events if e['stage']=='queue.accepted']
                self.assertEqual(len(accepted),100)
                self.assertEqual(len({e['job_id'] for e in accepted}),100)
                node.request_backup()
                import time
                deadline=time.monotonic()+15
                while node.backup_state().get('phase') not in ('completed','failed') and time.monotonic()<deadline:time.sleep(.02)
                self.assertEqual(node.backup_state()['phase'],'completed')
                second=sweep(config,1)
                client.json(client.session_path(config)+'/sweeps/batch',method='POST',payload={'items':[second]},status=201)
                node.request_final_backup();deadline=time.monotonic()+15
                while node.final_backup_state().get('phase') not in ('completed','failed') and time.monotonic()<deadline:time.sleep(.02)
                self.assertEqual(node.final_backup_state()['phase'],'completed')
                node.close()
                restored_dir=root/'restored';restored_dir.mkdir(mode=0o700)
                restored=PackageNode(binary,restored_dir,'restore-cli',restore_bundle=node.final_bundle)
                try:
                    config=Config(restored.start(),root/'unused-restore',run_id='actual-cli',points=4,workload_profile='macos-rc1')
                    actual=Client(config).json(Client(config).session_path(config)+'/sweeps?limit=100')
                    self.assertEqual(len(actual),2)
                    self.assertEqual(measurement_hash('sweep',actual[0]),measurement_hash('sweep',source))
                    self.assertEqual(measurement_hash('sweep',actual[1]),measurement_hash('sweep',second))
                    restored.close()
                finally:restored.close(expect_success=False)
            finally:node.close(expect_success=False)
