from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.ori_bench.scan_rate import compare, grid_windows


class ScanRateTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.export = Path(self.work.name) / 'sources.ndjson'
        self.band = dict(id='crossing', start_hz=797110000, stop_hz=800010000, step_hz=10000,
                         points=291, rbw_hz=30000)
        self.firmware = 'tinySA4_v1.4-235-gea71dfd\nHW Version:V0.4.5.1'
        normal = dict(device_id_sha256=hashlib.sha256(b'synthetic-analyzer').hexdigest(),
                      source='command_acknowledged', device_mode='input', lna=False,
                      attenuation_db='auto', calculation='off', ultra=False, rbw_hz=30000)
        self.paths = dict(normal=normal, ultra=dict(normal, ultra=True))
        self.reference = dict(format='ori-direct-scan-v2', band=self.band, passes=1,
                              points=291, windows=2, elapsed_seconds=1.8,
                              utc_elapsed_us=1800000, baud=9600, model='tinySA Ultra ZS405',
                              firmware=self.firmware, receive_paths=deepcopy(self.paths))

    def records(self, extra=0):
        result = [dict(format='openradioinspector-archive-ndjson', format_version=1, session_id='s')]
        for i in range(3 + extra):
            first, length, ultra = (0, 289, False) if i % 2 == 0 else (289, 2, True)
            settings = dict(adapter='tinysa-serial', simulated=False,
                            device_identity=dict(info='tinySA Ultra ZS405', firmware=self.firmware),
                            rbw_hz=30000, scan_serial_baud=9600, scan_band_id='crossing', scan_step_hz=10000,
                            scan_rx_evidence=dict(self.paths['ultra' if ultra else 'normal']),
                            scan_ultra_input=ultra, scan_ultra_source='command_acknowledged',
                            scan_window_count=2, scan_plan_id='plan', scan_plan_revision=1,
                            scan_window_index=i % 2, sequence=i + 1, receive_profile='tinysa-ultra-zs405-receive-v1')
            result.append(dict(kind='sweep', measurement=dict(session_id='s', stream_id='a', id=f'sweep-{i}',
                settings=settings, levels=[-41.25] * length, level_unit='dBm',
                frequencies_hz=[self.band['start_hz'] + 10000 * j for j in range(first, first + length)],
                interval=dict(started_at=f'2026-01-01T00:00:0{i}Z', ended_at=f'2026-01-01T00:00:0{i}.125Z'))))
        return result

    def measure(self, rows):
        self.export.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return compare(self.reference, self.export, 'a')

    def test_last_completed_commit_counts_with_idle_time_and_rebalanced_tail(self):
        report = self.measure(self.records(extra=2))
        self.assertTrue(report['passed'])
        self.assertEqual((report['points'], report['windows']), (291, 2))
        self.assertAlmostEqual(report['ratio'], .90)
        self.assertEqual(report['boundary_points'], 289)
        self.assertEqual(report['extra_sweeps_after_boundary'], 2)
        self.assertEqual(report['extra_points_after_boundary'], 291)
        self.assertEqual(report['exported_stream_points'], 871)
        self.reference.update(elapsed_seconds=1.7, utc_elapsed_us=1700000)
        self.assertFalse(self.measure(self.records())['passed'])
        self.reference['utc_elapsed_us'] = 1510000
        with self.assertRaisesRegex(ValueError, 'clocks disagree'):
            self.measure(self.records())

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
        rows = self.records(extra=2)
        with self.assertRaisesRegex(ValueError, 'missing, reordered or repeated'):
            self.measure([rows[0], *rows[3:]])

    def test_same_firmware_and_baud_are_insufficient_to_match_hardware_and_rx_settings(self):
        for key, value in (
            ('device_id_sha256', 'b' * 64), ('device_id_sha256', 'raw device ID'),
            ('ultra', False), ('ultra', 1), ('source', 'unknown'), ('device_mode', 'unknown'),
            ('lna', True), ('lna', 0), ('attenuation_db', 5), ('calculation', 'max'),
            ('rbw_hz', 10000),
        ):
            rows = self.records()
            rows[2]['measurement']['settings']['scan_rx_evidence'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.measure(rows)
        for key, value in (('scan_ultra_input', False), ('scan_ultra_source', 'unknown')):
            rows = self.records()
            rows[2]['measurement']['settings'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.measure(rows)

    def test_each_level_and_each_window_must_be_real_raw_evidence(self):
        for index in (1, 2, 3):
            for value in (float('nan'), float('inf'), float('-inf'), True, None, '-41.25'):
                rows = self.records()
                rows[index]['measurement']['levels'][-1] = value
                with self.subTest(index=index, level=value), self.assertRaises(ValueError):
                    self.measure(rows)
            rows = self.records()
            del rows[index]['measurement']['settings']['scan_rx_evidence']
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.measure(rows)

    def test_reject_incomplete_or_incompatible_direct_evidence(self):
        original = self.reference
        for problem in ('old_format', 'different_unit', 'missing_path', 'firmware', 'model', 'simulation'):
            self.reference = deepcopy(original)
            rows = self.records()
            if problem == 'old_format':
                self.reference['format'] = 'ori-direct-scan-v1'
            elif problem == 'different_unit':
                self.reference['receive_paths']['normal']['device_id_sha256'] = 'c' * 64
            elif problem == 'missing_path':
                del self.reference['receive_paths']['normal']
            elif problem == 'firmware':
                self.reference['firmware'] = self.firmware.splitlines()[0]
            elif problem == 'model':
                rows[1]['measurement']['settings']['device_identity']['info'] = 'another tinySA model'
            else:
                rows[2]['measurement']['settings']['replay'] = True
            with self.subTest(problem=problem), self.assertRaises(ValueError):
                self.measure(rows)

    def test_full_grid_contains_all_offered_points_exactly_once(self):
        windows = list(grid_windows(dict(start_hz=100000, points=129991)))
        frequency = 100000
        self.assertEqual(len(windows), 449)
        for first, points, ultra in windows:
            self.assertEqual(first, frequency)
            self.assertTrue(2 <= points <= 290)
            frequency += points * 10000
            self.assertIs(ultra, frequency - 10000 > 800000000)
        self.assertEqual(frequency - 10000, 1300000000)
