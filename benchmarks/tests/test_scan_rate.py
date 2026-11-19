import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.ori_bench.scan_rate import compare


class ScanRateTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.export = Path(self.work.name) / 'sources.ndjson'
        self.band = dict(id='vhf', start_hz=1000000, stop_hz=3900000, step_hz=10000,
                         points=291, rbw_hz=30000)
        self.reference = dict(format='ori-direct-scan-v1', band=self.band, passes=1,
                              points=291, windows=2, elapsed_seconds=1.8,
                              utc_elapsed_us=1800000, baud=9600, firmware='test-profile')

    def records(self):
        result = [dict(format='openradioinspector-archive-ndjson', format_version=1, session_id='s')]
        for i, (first, length) in enumerate(((0, 289), (289, 2), (0, 289))):
            settings = dict(adapter='tinysa-serial', simulated=False, device_identity=dict(firmware='test-profile'),
                            rbw_hz=30000, scan_serial_baud=9600, scan_band_id='vhf', scan_step_hz=10000,
                            scan_window_count=2, scan_plan_id='plan', scan_plan_revision=1,
                            scan_window_index=i % 2, sequence=i + 5, receive_profile='tinysa-ultra-zs405-receive-v1')
            result.append(dict(kind='sweep', measurement=dict(session_id='s', stream_id='a', id=f'sweep-{i}',
                settings=settings, levels=[-41.25] * length, level_unit='dBm',
                frequencies_hz=[1000000 + 10000 * j for j in range(first, first + length)],
                interval=dict(started_at=f'2026-01-01T00:00:0{i}Z', ended_at=f'2026-01-01T00:00:0{i}.125Z'))))
        return result

    def measure(self, rows):
        self.export.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return compare(self.reference, self.export, 'a')

    def test_last_completed_commit_counts_with_idle_time_and_rebalanced_tail(self):
        report = self.measure(self.records())
        self.assertTrue(report['passed'])
        self.assertEqual((report['points'], report['windows']), (291, 2))
        self.assertAlmostEqual(report['ratio'], .90)
        self.reference.update(elapsed_seconds=1.7, utc_elapsed_us=1700000)
        self.assertFalse(self.measure(self.records())['passed'])

    def test_rejects_partial_duplicate_drift_wrong_settings_or_clock(self):
        for problem in ('partial', 'duplicate', 'gap', 'wrong_rbw', 'synthetic', 'wrong_grid', 'clock', 'revision'):
            rows = self.records()
            sweep = rows[2]['measurement']
            if problem == 'partial':
                rows.pop()
            elif problem == 'duplicate':
                sweep['id'] = rows[1]['measurement']['id']
            elif problem == 'gap':
                sweep['settings']['sequence'] += 1
            elif problem == 'wrong_rbw':
                sweep['settings']['rbw_hz'] = 10000
            elif problem == 'synthetic':
                sweep['settings']['simulated'] = True
            elif problem == 'wrong_grid':
                sweep['frequencies_hz'][0] -= 10000
            elif problem == 'clock':
                sweep['interval']['started_at'] = '2025-12-31T23:59:59Z'
            else:
                sweep['settings']['scan_plan_revision'] = 2
            with self.subTest(problem=problem), self.assertRaises(ValueError):
                self.measure(rows)
