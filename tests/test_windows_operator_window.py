"""Portable checks for the installed Windows operator-window evidence."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from deploy.windows import capture_installed_window as capture


class OperatorWindowTests(unittest.TestCase):
    def test_only_owned_fixture_adapters_and_sources_are_accepted(self):
        fixture = json.loads(capture.FIXTURE.read_bytes())
        self.assertEqual(fixture['session']['id'], 'windows-training')
        self.assertEqual({s['settings']['adapter'] for s in fixture['session']['streams']},
                         {'tinysa-sim', 'ardv1-sim'})
        with self.assertRaisesRegex(capture.Unavailable, 'source_evidence'):
            capture.verify_sources([{'stream_id': 'spectrum'}] * 12, [])

    def test_live_or_serial_core_is_refused_before_any_fixture_is_written(self):
        from io import BytesIO
        for mode, serial in [('live', False), ('replay', True)]:
            capabilities = {'candidate': {'mode': mode}, 'features': {'explicit_serial_adapters': serial}}
            with self.subTest(mode=mode, serial=serial), patch.object(capture, 'build_opener') as client:
                client.return_value.open.return_value = BytesIO(json.dumps({'data': capabilities}).encode())
                with self.assertRaisesRegex(capture.Unavailable, 'not_simulator_only'):
                    capture.prepare_fixture('http://127.0.0.1:1')
                client.return_value.open.assert_called_once()
                self.assertEqual(client.return_value.open.call_args.args[0].get_method(), 'GET')

    def test_non_windows_records_reason_without_an_image(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            receipt = directory / (capture.BASENAME + '.json')
            receipt.write_text('{"expected_revision":"test"}')
            with patch.object(sys, 'argv', ['capture_installed_window.py', 'capture', '--core', 'unused',
                                            '--desktop', 'unused', '--output', root]), patch.object(sys, 'platform', 'linux'):
                capture.main()
            result = json.loads(receipt.read_bytes())
            self.assertEqual((result['status'], result['reason']), ('unavailable', 'windows_required'))
            self.assertEqual(list(directory.iterdir()), [receipt])

    def test_fresh_install_capture_keeps_the_monitoring_failure(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            with self.assertRaisesRegex(capture.Unavailable, 'monitoring_wait'):
                capture.select_capture(directory, 'monitoring_wait')
            (directory / 'fresh_install_connection.json').write_text('{"width": 1, "height": 1, "dpi": 96}')
            png = directory / 'fresh_install_connection.png'
            png.write_bytes(b'incomplete')
            with self.assertRaisesRegex(capture.Unavailable, 'png_geometry_mismatch'):
                capture.select_capture(directory, 'monitoring_wait')

            def chunk(kind, data):
                return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

            png.write_bytes(b'\x89PNG\r\n\x1a\n' +
                            chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) +
                            chunk(b'IDAT', zlib.compress(b'\x00\xff\xff\xff')) + chunk(b'IEND', b''))
            scene, details, source = capture.select_capture(directory, 'monitoring_wait')
            self.assertEqual(scene, 'fresh_install_connection')
            self.assertEqual(details['monitoring_unavailable_reason'], 'monitoring_wait')
            self.assertEqual(source.name, 'fresh_install_connection.png')


    def test_replay_command_requires_explicit_binary(self):
        environment = os.environ.copy()
        environment.pop('ORI_REPLAY_TEST_CORE', None)
        result = subprocess.run([sys.executable, capture.__file__, 'check-replay'], env=environment,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires ORI_REPLAY_TEST_CORE', result.stderr)
