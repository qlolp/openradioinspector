"""Owner-admitted deadlines, not fixed controller timeouts, govern recovery."""
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import Mock, patch
from benchmarks.ori_bench.mac_acceptance.node import PackageNode
from benchmarks.ori_bench.config import BenchmarkError


class BackupDeadlineTests(unittest.TestCase):
    def test_physical_duty_requires_measured_allocation_and_room_for_both_recovery_files(self):
        from benchmarks.ori_bench.mac_acceptance.budget import require_duty_storage
        complete = dict(ready=True, storage_id='node-archive', backup_reserve_requested=True,
                        restore_reserve_requested=True, backup_reserve_bytes=5000, restore_reserve_bytes=5000,
                        required_free_bytes=16000, filesystem_free_bytes=16000, shortfall_bytes=0,
                        bytes_per_second=1.5, physical_growth_bytes_per_second=0.0,
                        forecast_seconds=43200, estimated_retention_seconds=43200)
        require_duty_storage(complete)
        for change in [dict(restore_reserve_requested=False), dict(restore_reserve_bytes=0),
                       dict(physical_growth_bytes_per_second=None), dict(forecast_seconds=3600),
                       dict(required_free_bytes=16001), dict(shortfall_bytes=1), dict(bytes_per_second=True)]:
            with self.subTest(change=change), self.assertRaises(BenchmarkError):
                require_duty_storage(complete | change)

    def test_initial_wal_sampling_waits_for_a_fresh_report_without_repeating_a_shortfall(self):
        from benchmarks.ori_bench.mac_acceptance import budget
        warming = dict(forecast_status='measuring_allocation', next_recheck_seconds=2)
        final = dict(forecast_status='insufficient_space', shortfall_bytes=1)
        fetch = Mock(side_effect=[warming, warming, final])
        with patch.object(budget, 'sleep') as wait:
            self.assertEqual(budget.sample_duty_storage(fetch), final)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([call.args for call in wait.call_args_list], [(2,), (2,)])

    def test_controller_waits_beyond_old_600_seconds_for_admitted_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            node=PackageNode(Path(sys.executable),Path(directory),'budget-fixture')
            started=node.receipts/'final-backup-started.json'
            finished=node.receipts/'final-backup-finished.json'
            started.write_text(json.dumps({'phase':'running','at_ns':1_000_000_000,
                'budget':{'snapshot_bytes':2636263424,'copy_timeout_seconds':438,'total_timeout_seconds':1252}}))
            def complete(_seconds):
                finished.write_text(json.dumps({'phase':'completed','manifest':{'counts':{'sweeps':18000,'observations':18000}}}))
            with patch('benchmarks.ori_bench.mac_acceptance.budget.monotonic',return_value=701.0),patch('benchmarks.ori_bench.mac_acceptance.budget.monotonic_ns',return_value=701_000_000_000),patch('benchmarks.ori_bench.mac_acceptance.budget.sleep',side_effect=complete):
                result=node.wait_for_backup(final=True)
            self.assertEqual(result['phase'],'completed')
            self.assertEqual(result['manifest']['counts']['sweeps'],18000)

    def test_restore_timeout_uses_size_of_sealed_manifest(self):
        from benchmarks.ori_bench.mac_acceptance.node import recovery_timeout
        self.assertEqual(recovery_timeout({'archive':{'bytes':2636263424}}),1252)
        self.assertEqual(recovery_timeout({'archive':{'bytes':4096}}),600)

    def test_invalid_snapshot_size_cannot_create_an_unbounded_restore_wait(self):
        from benchmarks.ori_bench.mac_acceptance.node import recovery_timeout
        for size in [0,True,-1,16*1024**3+1,2**64-1,'2636263424']:
            with self.subTest(size=size),self.assertRaises(BenchmarkError):
                recovery_timeout({'archive':{'bytes':size}})

    def test_expired_owner_budget_fails_without_restarting_the_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            node=PackageNode(Path(sys.executable),Path(directory),'budget-fixture')
            (node.receipts/'backup-started.json').write_text(json.dumps({'phase':'running','at_ns':1_000_000_000,
                'budget':{'snapshot_bytes':2636263424,'copy_timeout_seconds':438,'total_timeout_seconds':1252}}))
            with patch('benchmarks.ori_bench.mac_acceptance.budget.monotonic',return_value=3000.0),patch('benchmarks.ori_bench.mac_acceptance.budget.monotonic_ns',return_value=3_000_000_000_000):
                with self.assertRaises(BenchmarkError) as caught:node.wait_for_backup()
            self.assertEqual(caught.exception.code,'backup_timeout')
