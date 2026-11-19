"""Release-fixture contract and original-Job cleanup checks."""
import copy
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from benchmarks.ori_bench.concurrent import BackgroundCandidate
from benchmarks.ori_bench.config import BenchmarkError, Config
from benchmarks.ori_bench.endurance_node import ROOT
from benchmarks.ori_bench.paged_history import PagedHistoryWork, active, verify_page
from benchmarks.ori_bench.source import sweep
from benchmarks.ori_bench.windows_owner import WindowsOwnedCommand


class PagedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.config = Config('http://127.0.0.1:1', Path('/unused'), run_id='paged', points=16)
        rows = []
        for index in (5, 4):
            value = sweep(self.config, index)
            peak = max(value['levels'])
            row = {name: value[name] for name in ('id', 'session_id', 'stream_id', 'interval', 'level_unit')}
            row.update(point_count=16, peak_level=peak, peak_frequency_hz=value['frequencies_hz'][value['levels'].index(peak)],
                       min_frequency_hz=value['frequencies_hz'][0], max_frequency_hz=value['frequencies_hz'][-1])
            rows.append(row)
        self.page = dict(items=rows, next_cursor='cursor')

    def test_cursor_order_source_precision_and_matching_stream_are_required(self):
        self.assertEqual(verify_page(self.config, self.page, 6), ([5, 4], 'cursor'))
        for key, value in (('id', 'another-sweep-000000005'), ('stream_id', 'foreign'),
                           ('peak_frequency_hz', 42), ('peak_level', 0.0), ('levels', [])):
            page = copy.deepcopy(self.page)
            page['items'][0][key] = value
            with self.subTest(key=key), self.assertRaises(BenchmarkError):
                verify_page(self.config, page, 6)
        with self.assertRaises(BenchmarkError):
            verify_page(self.config, self.page, 5)
        with self.assertRaises(BenchmarkError):
            verify_page(self.config, dict(items=[], next_cursor=None))

    def test_real_backup_export_and_qos_evidence_is_required_for_both_page_boundaries(self):
        sample = dict(backup=dict(phase='running'), export=dict(phase='streaming', verified_records=2),
                      qos=dict(registered_work=2, cleanup_failed=False, acquisition_pressure=False,
                               cross_process_logical_io=False))
        self.assertTrue(active(sample))
        for changed in (dict(backup=dict(phase='requested')), dict(backup=dict(phase='completed')),
                        dict(export=dict(phase='streaming', verified_records=0)),
                        dict(qos=dict(sample['qos'], registered_work=1)),
                        dict(qos=dict(sample['qos'], cleanup_failed=True))):
            self.assertFalse(active(sample | changed))

    def test_page_must_be_inside_actual_verified_producer_offers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'offers.sqlite'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE offers (kind, offered_ns, verified, status)')
            db.executemany('INSERT INTO offers VALUES (?, ?, ?, ?)', [
                ('sweep', 100, 1, 'accepted'), ('sweep', 300, 1, 'accepted'), ('sweep', 400, 0, 'accepted')])
            db.commit()
            db.close()
            work = PagedHistoryWork(self.config, None, 0)
            work.report['overlap'] = dict(started_monotonic_ns=101, finished_monotonic_ns=299)
            work.check_production_overlap(path)
            self.assertEqual(work.report['overlap']['verified_offers_bracket_ns'], [100, 300])
            work.report['overlap']['finished_monotonic_ns'] = 301
            with self.assertRaises(BenchmarkError):
                work.check_production_overlap(path)

    def test_paged_history_needs_explicit_selection_and_excludes_worker_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = Path(sys.executable).resolve()
            with self.assertRaises(BenchmarkError):
                BackgroundCandidate(binary, root, None)
            with self.assertRaises(BenchmarkError):
                BackgroundCandidate(binary, root, binary, workload_profile='paged-history')
            with self.assertRaises(BenchmarkError):
                BackgroundCandidate(binary, root, None, best_effort=True, workload_profile='paged-history')
            self.assertIsNone(BackgroundCandidate(binary, root, None, workload_profile='paged-history').python)


@unittest.skipUnless(sys.platform == 'win32', 'native Windows Job accounting')
class WindowsJobFixtureTests(unittest.TestCase):
    def test_guard_join_and_lingering_descendant_are_distinguished(self):
        with tempfile.TemporaryDirectory() as temp, (Path(temp) / 'child.log').open('wb') as log:
            def launch(code):
                return WindowsOwnedCommand.start([sys.executable, '-c', code], environment=dict(os.environ),
                                                 root=ROOT, log=log, shutdown_timeout=5)
            command = launch('pass')
            self.assertEqual(command.wait(10), 0)
            command.release(5)
            self.assertIsNone(command.job.handle)
            command = launch("import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])")
            try:
                self.assertEqual(command.wait(10), 0)
                with self.assertRaises(BenchmarkError):
                    command.release(.15)
                self.assertGreater(command.job.active(), 0)
            finally:
                command.stop(5)
            self.assertIsNone(command.job.handle)
