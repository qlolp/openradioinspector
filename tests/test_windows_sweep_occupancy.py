"""Synthetic core/client parity, frozen requests and lossless local exports."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch
from radioinspector.sweep_occupancy import summarize_occupancy
from radioinspector.windows.client import AverageChangeResult, PlatformClient
from radioinspector.windows.sweep_occupancy import validate_occupancy, save_occupancy_file, occupancy_text
from tests.test_windows_sweep_context import panel

FIXTURE = Path(__file__).parent / 'fixtures/sweep-occupancy-v1.json'


def result():
    return json.loads(FIXTURE.read_text())['data']


def source(data):
    return dict(data['items'][2], session_id=data['session_id'], stream_id=data['stream_id'], frequencies_hz=data['frequencies_hz'])


def loaded_panel():
    p = panel(); p.threshold = Mock(); p.threshold.get.return_value = '-97'
    p.exporting = False; p.occupancy_button = Mock(); p.export_button = Mock()
    data = result(); p.app.snapshot.sweeps = [source(data)]
    p.analyze(True)
    p.future.set_result(AverageChangeResult(data, json.dumps(data, ensure_ascii=False)))
    p.finish(p.generation)
    return p


class OccupancyClientTests(unittest.TestCase):
    def test_golden_matches_core_and_preserves_all_bins(self):
        data = result()
        self.assertEqual(data['occupancy'], summarize_occupancy(data, -97))
        self.assertIs(validate_occupancy(data, ('session', 's'), source(data), 2, 2, -97), data)
        self.assertIn('2/5', occupancy_text(data))
        self.assertIn('[2] 102 Гц', occupancy_text(data))

    def test_malformed_summary_is_rejected(self):
        for key, values in {
            'method': ['rf_time'], 'comparison': ['greater_or_equal'], 'threshold_dbm': [-96, True],
            'sample_count': [True, 0, 4], 'exceedance_counts': [[True]*3, [6]*3, [2]],
            'sample_fractions': [[float('nan')]*3, [0]*3], 'rf_time_occupancy': [0],
            'acquisition_span_seconds': [-1, float('inf')], 'acquisition_covered_seconds': [9],
            'acquisition_gap_seconds': [0], 'acquisition_gap_count': [True, 5],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    data = result(); data['occupancy'][key] = value
                    validate_occupancy(data, ('session','s'), source(data), 2, 2, -97)

    def test_request_validation_and_exact_endpoint(self):
        client = PlatformClient('http://localhost:8080')
        with patch.object(client, '_request', return_value={}) as request:
            client.sweep_occupancy('a/b', 'sweep-3', 2, 2, -97)
            self.assertEqual(request.call_args.args[:2], ('POST', '/sessions/a%2Fb/sweep-occupancy'))
            self.assertTrue(request.call_args.kwargs['preserve_data_json'])
            for value in (True, None, float('nan'), float('inf'), 10**400):
                with self.assertRaises(ValueError): client.sweep_occupancy('a', 'b', 2, 2, value)
            self.assertEqual(request.call_count, 1)

    def test_singleflight_and_frozen_result_clear(self):
        p = loaded_panel()
        self.assertEqual(p.data['occupancy']['exceedance_counts'], [2,2,2])
        p.executor.submit.assert_called_once_with(p.app.client.sweep_occupancy, 'session', 'sweep-3', 2, 2, -97)
        self.assertIsNotNone(p.result_json)
        p.app.snapshot.sweeps = []; p.update()
        self.assertIsNotNone(p.data)
        p.clear(); self.assertIsNone(p.result_json); self.assertIsNone(p.data)
        p.export_button.configure.assert_called_with(state='disabled')

    def test_export_lossless_atomic_no_clobber(self):
        data = result(); raw = json.dumps(data, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'result.json'
            save_occupancy_file(path, data, raw)
            text = path.read_text(); self.assertIn('9007199254740993', text)
            self.assertIn(raw, text)
            self.assertEqual(json.loads(text)['data'], data)
            with self.assertRaises(FileExistsError): save_occupancy_file(path, data, raw)
            self.assertEqual(path.read_text(), text)
            self.assertEqual(list(Path(directory).iterdir()), [path])
            with self.assertRaises(ValueError): save_occupancy_file(path, data, '{}', True)

    def test_export_dialog_cancel_and_stale_generation(self):
        for action in ('cancel', 'clear', 'disconnect', 'close'):
            p = loaded_panel()
            def choose(**kwargs):
                if action != 'cancel': getattr(p, action)()
                return '' if action == 'cancel' else '/tmp/should-never-be-written.json'
            with patch('tkinter.filedialog.asksaveasfilename', side_effect=choose), patch('radioinspector.windows.sweep_occupancy.save_occupancy_file') as save:
                p.export_result(); save.assert_not_called()
                self.assertFalse(p.exporting)

    def test_panel_exports_transport_result_without_reencoding(self):
        p = loaded_panel()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'result.json'
            with patch('tkinter.filedialog.asksaveasfilename', return_value=str(path)):
                p.export_result()
            self.assertEqual(json.loads(path.read_text())['data'], dict(p.data))
            self.assertIn(p.result_json, path.read_text())
