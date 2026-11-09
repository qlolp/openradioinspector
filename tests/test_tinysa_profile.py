"""Application policy tests. Scripted identities are not hardware evidence."""
from __future__ import annotations

import unittest
from unittest.mock import Mock

from radioinspector.device_workers import WorkerError, TinySASimulator
from radioinspector.serial_instruments import TinySASerialAdapter
from radioinspector.session_service import SessionService
from radioinspector.tinysa_profile import FIRMWARE, PROFILE_ID, profile_description, validate_receive_settings
from tests.test_serial_instruments import serial_session, TinySATransport


class TinySAReceiveProfileTests(unittest.TestCase):
    def setUp(self):
        self.service = SessionService()
        self.addCleanup(self.service.close)

    def stream(self, **settings):
        payload = serial_session()
        payload['streams'][0]['settings'].update(settings)
        return self.service.create_session(payload).streams[0]

    def test_invalid_settings_rejected_before_port_open(self):
        for name, values in {
            'start_hz': [True, 0, 99_999, 800_000_001, 100_000.0, '100000'],
            'stop_hz': [100_000_000, 800_000_001, 20_000_000_000],
            'points': [True, 1, 291, 4096, 2.0],
            'rbw_hz': [True, 200, 20_000, 850_000, 1_000_000_000],
            'receive_profile': ['', None, 'unknown', [], {}],
        }.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    factory = Mock()
                    with self.assertRaises(ValueError):
                        TinySASerialAdapter(self.stream(**{name: value}), transport_factory=factory)
                    factory.assert_not_called()

    def test_application_boundaries_and_default_profile(self):
        for points in (2, 101, 201, 290):
            for rbw in (10_000, 30_000):
                result = validate_receive_settings(dict(start_hz=100_000, stop_hz=800_000_000,
                                                       points=points, rbw_hz=rbw))
                self.assertEqual(result['points'], points)
                self.assertEqual(result['rbw_hz'], rbw)
        first = profile_description()
        first['rbw_requests_hz'].append(20_000)
        self.assertEqual(profile_description()['rbw_requests_hz'], [10_000, 30_000])

    def test_unknown_model_or_firmware_fails_before_setup_without_retry(self):
        for info, firmware in (
            ('tinySA Ultra ZS405', 'tinySA4_v1.5-new'),
            ('tinySA Ultra ZS405', 'prefix ' + FIRMWARE),
            ('tinySA Ultra+ ZS407', FIRMWARE),
            ('not tinySA Ultra ZS405', FIRMWARE),
        ):
            with self.subTest(info=info, firmware=firmware):
                transport = TinySATransport.valid()
                transport.responses['info'] = [info + '\r\nch>']
                transport.responses['version'] = [firmware + '\r\nch>']
                adapter = TinySASerialAdapter(self.stream(), transport=transport)
                self.addCleanup(adapter.close)
                with self.assertRaisesRegex(WorkerError, 'профилем'):
                    adapter.poll()
                self.assertEqual(transport.sent, [b'info\r', b'version\r', b'deviceid\r'])
                with self.assertRaisesRegex(WorkerError, 'перезапуск'):
                    adapter.poll()
                self.assertEqual(len(transport.sent), 3)
                self.assertEqual(adapter.details()['device_identity']['firmware'], firmware)
                self.assertEqual(adapter.details()['settings_source'], 'unknown')

    def test_provenance_is_requested_and_acknowledged_never_applied(self):
        adapter = TinySASerialAdapter(self.stream(), transport=TinySATransport.valid())
        self.addCleanup(adapter.close)
        self.assertEqual(adapter.details()['device_identity'], {})
        sweep = adapter.poll()
        self.assertEqual(sweep.settings['receive_profile'], PROFILE_ID)
        self.assertEqual(sweep.settings['device_identity']['firmware'], FIRMWARE)
        self.assertEqual(sweep.settings['rbw_hz'], 10_000)  # Legacy request retained.
        self.assertEqual(sweep.settings['rbw_requested_hz'], 10_000)
        self.assertNotIn('rbw_applied_hz', sweep.settings)
        self.assertEqual(sweep.settings['rbw_applied_state'], 'unknown')
        from radioinspector.sweep_comparison import validate_sweep_compatibility
        validate_sweep_compatibility(sweep.session_id, sweep, sweep)
        self.assertFalse(sweep.settings['settings_readback'])
        self.assertEqual(sweep.settings['settings_source'], 'command_acknowledged')
        self.assertEqual(sweep.settings['hardware_validation'], 'not_run')
        self.assertEqual(adapter.details()['receive_profile']['id'], PROFILE_ID)
        adapter.close()
        self.assertEqual(adapter.details()['settings_source'], 'unknown')
        self.assertEqual(sweep.settings['settings_source'], 'command_acknowledged')

    def test_30khz_command_and_archive_round_trip(self):
        import tempfile
        from pathlib import Path
        from dataclasses import replace
        from radioinspector.session_store import SQLiteSessionStore
        from radioinspector.sweep_comparison import (
            ComparisonIncompatibleError, validate_sweep_compatibility,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'profile.sqlite')
            service = SessionService(store=SQLiteSessionStore(path))
            try:
                payload = serial_session()
                payload['streams'][0]['settings']['rbw_hz'] = 30_000
                session = service.create_session(payload)
                transport = TinySATransport.valid()
                transport.responses['rbw 30'] = transport.responses.pop('rbw 10')
                adapter = TinySASerialAdapter(session.streams[0], transport=transport)
                try:
                    sweep = adapter.poll()
                finally:
                    adapter.close()
                self.assertIn(b'rbw 30\r', transport.sent)
                self.assertNotIn(b'rbw 10\r', transport.sent)
                self.assertEqual(sweep.settings['rbw_requested_hz'], 30_000)
                service.ingest_sweep(sweep)
                legacy = replace(sweep, id='legacy-sweep', settings={'rbw_hz': 30_000})
                service.ingest_sweep(legacy)
            finally:
                service.close()
            store = SQLiteSessionStore(path)
            try:
                restored = store.get_sweep(sweep.id)
                restored_legacy = store.get_sweep(legacy.id)
                self.assertEqual(restored.settings, sweep.settings)
                self.assertEqual(restored_legacy.settings, legacy.settings)
                validate_sweep_compatibility(session.id, restored, restored)
                with self.assertRaises(ComparisonIncompatibleError):
                    validate_sweep_compatibility(session.id, restored, restored_legacy)
            finally:
                store.close()

    def test_wrong_scan_edges_are_not_archived_or_retried(self):
        transport = TinySATransport.valid()
        command = 'scan 100M 101M 3 3'
        transport.responses[command] = ['100000001 -90 0\r\n100500000 -80 0\r\n101000000 -70 0\r\nch>']
        adapter = TinySASerialAdapter(self.stream(), transport=transport)
        self.addCleanup(adapter.close)
        with self.assertRaisesRegex(WorkerError, 'границы scan'):
            adapter.poll()
        self.assertEqual(adapter.details()['settings_source'], 'unknown')
        with self.assertRaisesRegex(WorkerError, 'перезапуск'):
            adapter.poll()
        self.assertEqual(transport.sent.count((command + '\r').encode()), 1)

    def test_simulator_remains_unrestricted_by_serial_profile(self):
        stream = self.stream(stop_hz=5_000_000_000, points=4096, rbw_hz=100_000)
        adapter = TinySASimulator(stream)
        self.assertEqual(adapter.points, 4096)
        self.assertEqual(adapter.stop_hz, 5_000_000_000)
