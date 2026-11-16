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
    def test_real_owner_queue_backup_and_restore_preserve_the_original(self):
        binary=Path(os.environ['ORI_CORE_TEST_BINARY']).resolve(strict=True)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);output=root/'node';output.mkdir(mode=0o700)
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
