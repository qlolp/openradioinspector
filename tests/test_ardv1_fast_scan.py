"""Fast scan protocol and failure isolation; no physical receiver required."""
import unittest
from radioinspector.device_workers import DeviceWorker, WorkerError
from tests.test_device_workers import wait_until
from radioinspector.serial_instruments import ARDV1SerialAdapter
from radioinspector.session_service import SessionService
from tests.test_serial_instruments import serial_session, ScriptedTransport


class FastScanTests(unittest.TestCase):
    def adapter(self, *, serial='RNsynthetic', **scan):
        payload = serial_session()
        settings = payload['streams'][1]['settings']
        settings.pop('period_s')
        settings.update(receiver_profile='ardv1-monitor-v1', fast_scan={
            'start_hz': 100000000, 'stop_hz': 100025010, 'step_hz': 25000,
            'settle_s': 0, **scan})
        self.service = SessionService()
        self.addCleanup(self.service.close)
        stream = self.service.create_session(payload).streams[1]
        transport = ScriptedTransport({
            'RE1': ['20'], 'VR': ['20VR2504A'], 'RN': ['20' + serial],
            'VFA': ['20'], 'MD0F0': ['20'],
            'RF0100.00000': ['20'], 'RF0100.02500': ['20'], 'RF0100.02501': ['20'],
            'RX': [f'20RX VFA RF{f} ST025.00 MD0F0 LM0551'
                   for f in ('0100.00000', '0100.02500', '0100.02501')]})
        return ARDV1SerialAdapter(stream, transport=transport), transport

    def native_grid(self, **scan):
        adapter, transport = self.adapter(serial='RN09522001', settle_s=0.015, **scan)
        transport.responses.update({
            'ST': ['20ST010.00', '20ST010.00'],
            'SH': ['20SH050.00', '20SH000.00', '20SH050.00'],
            'VFA RF0100.00000 ST025.00 SH000.00 MD0F0': ['20'],
            'ZK': ['20'],
            'VFA ST010.00 SH050.00': ['20'],
            'EX': ['20'],
        })
        return adapter, transport

    def test_verified_vfo_steps_and_absolute_final_point_restore_raster(self):
        adapter, wire = self.native_grid()
        rows = [adapter.poll() for _ in range(3)]
        self.assertEqual([x.frequency_hz for x in rows], [100_000_000, 100_025_000, 100_025_010])
        self.assertEqual([x.metadata['scan_tune_command'] for x in rows], ['RF', 'ZK', 'RF'])
        self.assertEqual([x.metadata['frequency_readback'] for x in rows], [True] * 3)
        self.assertEqual(wire.sent.count(b'ZK\r'), 1)
        self.assertNotIn(b'RF0100.02500\r', wire.sent)
        self.assertIn(b'RF0100.02501\r', wire.sent)
        adapter.close()
        self.assertIn(b'VFA ST010.00 SH050.00\r', wire.sent)
        self.assertTrue(adapter.details()['scan_grid_restored'])

    def test_unexpected_vfo_step_fails_closed_without_retry(self):
        adapter, wire = self.native_grid()
        wire.responses['RX'][1] = '20RX VFA RF0100.02500 ST050.00 MD0F0 LM0551'
        adapter.poll()
        with self.assertRaisesRegex(WorkerError, 'шаг'):
            adapter.poll()
        sent = list(wire.sent)
        adapter.close()
        self.assertEqual(wire.sent, sent)
        self.assertFalse(adapter.details()['scan_grid_restored'])

    def test_unknown_code_10_cannot_acknowledge_our_step(self):
        adapter, wire = self.native_grid()
        wire.responses['ZK'] = ['10']
        adapter.poll()
        with self.assertRaises(WorkerError):
            adapter.poll()
        self.assertEqual(wire.sent.count(b'RX\r'), 1)

    def test_off_grid_uses_absolute_tuning_even_on_verified_board(self):
        adapter, wire = self.native_grid(start_hz=100_000_010)
        wire.responses['RF0100.00001'] = ['20']
        wire.responses['RX'][0] = '20RX VFA RF0100.00001 ST025.00 MD0F0 LM0551'
        self.assertEqual(adapter.poll().metadata['scan_tune_command'], 'RF')
        self.assertNotIn(b'ST\r', wire.sent)
        self.assertNotIn(b'ZK\r', wire.sent)

    def test_complete_grid_and_endpoint_readback(self):
        adapter, transport = self.adapter()
        rows = [adapter.poll() for _ in range(3)]
        self.assertEqual([r.frequency_hz for r in rows], [100000000, 100025000, 100025010])
        self.assertTrue(all(r.metadata['frequency_readback'] for r in rows))
        self.assertTrue(all(r.level_unit == 's_meter_count' and r.level == 55 for r in rows))
        self.assertEqual(transport.sent.count(b'VFA\r'), 1)
        self.assertEqual(transport.sent.count(b'MD0F0\r'), 1)
        self.assertEqual(adapter.period_s, 0)
        self.assertTrue(adapter.instrument_idle())
        self.assertEqual(adapter.details()['scan_completed_points'], 3)
        before = list(transport.sent)
        with self.assertRaises(WorkerError): adapter.poll()
        self.assertEqual(transport.sent, before)

    def test_bad_or_stale_status_taints_without_retry(self):
        for raw in ('20RX VFA RF0099.00000 ST025.00 MD0F0 LM0551',
                    '20RX VFA RF0100.00000 ST025.00 MD0F1 LM0551',
                    '21RX VFA RF0100.00000 ST025.00 MD0F0 LM0551',
                    '20RX VFA RF0100.00000 ST025.00 MD0F0 LM0559', '20', '20LM0551'):
            with self.subTest(raw=raw):
                adapter, transport = self.adapter()
                transport.responses['RX'] = [raw]
                with self.assertRaises(WorkerError): adapter.poll()
                before = list(transport.sent)
                with self.assertRaises(WorkerError): adapter.poll()
                self.assertEqual(transport.sent, before)
                self.assertEqual(adapter.details()['scan_completed_points'], 0)
                self.assertTrue(adapter.details()['restart_required'])

    def test_failed_rf_never_queries_level(self):
        adapter, transport = self.adapter()
        transport.responses['RF0100.00000'] = ['50']
        with self.assertRaises(WorkerError): adapter.poll()
        self.assertNotIn(b'RX\r', transport.sent)

    def test_invalid_settings_fail_before_open(self):
        for scan in ({'step_hz': 0}, {'step_hz': True}, {'step_hz': 11},
                     {'stop_hz': 99999990}, {'settle_s': float('nan')},
                     {'settle_s': -1}, {'start_hz': 0}):
            with self.subTest(scan=scan), self.assertRaises((ValueError, WorkerError)):
                self.adapter(**scan)

    def test_manual_tune_rejected_without_disrupting_scan(self):
        adapter, transport = self.adapter()
        with self.assertRaises(WorkerError):
            adapter.execute('tune', {'frequency_hz': 145500000})
        self.assertEqual(transport.sent, [])
        self.assertEqual(adapter.poll().frequency_hz, 100000000)

    def test_single_point(self):
        adapter, _ = self.adapter(stop_hz=100000000)
        adapter.poll()
        self.assertTrue(adapter.instrument_idle())

    def test_worker_archives_each_point_once_then_releases_port(self):
        adapter, transport = self.adapter()
        self.service.start_session(adapter.stream.session_id)
        worker = DeviceWorker(self.service, adapter)
        self.addCleanup(worker.stop)
        worker.start()
        wait_until(lambda: worker.status()['measurements'] == 3, 'scan committed')
        rows = self.service.list_observations(adapter.stream.session_id)
        self.assertEqual(len(rows), 3)
        self.assertTrue(worker.status()['current']['scan_complete'])
        wait_until(lambda: not worker.alive, 'finite scan closed')
        self.assertFalse(worker.alive)
        self.assertTrue(transport.closed)
        self.assertEqual(transport.sent.count(b'RX\r'), 3)

    def test_archive_failure_stops_without_skipping_point(self):
        adapter, transport = self.adapter()
        self.service.start_session(adapter.stream.session_id)
        def fail(_):
            raise RuntimeError('synthetic archive failure')
        self.service.ingest_observation = fail
        worker = DeviceWorker(self.service, adapter)
        self.addCleanup(worker.stop)
        worker.start()
        wait_until(lambda: not worker.alive, 'archive failure stopped worker')
        self.assertEqual(worker.status()['measurements'], 0)
        self.assertIn('synthetic archive failure', worker.status()['last_error'])
        self.assertNotIn(b'RF0100.02500\r', transport.sent)

    def test_force_stop_interrupts_settle(self):
        adapter, transport = self.adapter(settle_s=1)
        self.service.start_session(adapter.stream.session_id)
        worker = DeviceWorker(self.service, adapter)
        self.addCleanup(worker.stop)
        worker.start()
        wait_until(lambda: b'RF0100.00000\r' in transport.sent, 'tuned')
        worker.stop(mode='force')
        self.assertFalse(worker.alive)
        self.assertEqual(worker.status()['measurements'], 0)
        self.assertNotIn(b'RX\r', transport.sent)


if __name__ == '__main__':
    unittest.main()
