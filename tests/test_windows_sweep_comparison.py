import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from radioinspector.windows.client import PlatformClient
from radioinspector.windows.sweep_comparison import eligible_sweeps, comparison_text, ComparisonPanel


class ComparisonTests(unittest.TestCase):
    def test_encoded_get(self):
        client = PlatformClient('http://localhost:8770')
        client._request = Mock(return_value={'points': 1})
        self.assertEqual(client.compare_sweeps('a/b', 'r&1', 't?2'), {'points': 1})
        client._request.assert_called_once_with('GET', '/sessions/a%2Fb/sweep-comparison?reference_id=r%261&target_id=t%3F2')

    def test_source_filter(self):
        app = SimpleNamespace(session_id='session', selected_stream_id=lambda: 's', snapshot=SimpleNamespace(session={'id': 'session'}, spectrum_stream_id='s', sweeps=[{'id': 'a', 'level_unit': 'dBm'}, {'id': 'b', 'level_unit': 's_meter_count'}]))
        self.assertEqual([s['id'] for s in eligible_sweeps(app)], ['a'])
        app.snapshot.spectrum_stream_id = 'other'
        self.assertEqual(eligible_sweeps(app), [])

    def test_units_and_negative_maximum(self):
        item = {'id': 'r', 'interval': {'started_at': '2026-01-01T00:00:00Z'}, 'level_unit': 'dBm'}
        data = {'reference': item, 'target': dict(item, id='t'), 'max_increase': {'delta_db': -1, 'frequency_hz': 20}, 'min_change': {'delta_db': -4, 'frequency_hz': 10}}
        text = comparison_text(data)
        for expected in ('reference: r', 'target: t', 'UTC', 'dBm', '-1 dB', 'Максимальное изменение'):
            self.assertIn(expected, text)

    def test_stale_completion_discarded(self):
        panel = ComparisonPanel.__new__(ComparisonPanel)
        panel.closed = False
        panel.update = Mock()
        panel.future = Mock()
        panel.future.done.return_value = True
        panel.generation = 2
        panel.button = Mock()
        panel.info = Mock()
        panel.finish(1)
        panel.info.set.assert_not_called()
        panel.future.result.assert_not_called()

from concurrent.futures import Future
from time import monotonic
from copy import deepcopy
from math import isfinite
from radioinspector.windows.sweep_comparison import validate_comparison


def response():
    item = {'id': 'r', 'interval': {'started_at': '2026-01-01T00:00:00Z', 'ended_at': '2026-01-01T00:00:01Z'}, 'level_unit': 'dBm', 'levels': [-40, -50, -60], 'settings': {'rbw_hz': 1000}}
    return {'session_id': 'session', 'stream_id': 's', 'reference': item, 'target': dict(item, id='t'), 'points': 3, 'frequencies_hz': [0, 1, 100], 'delta_levels': [-1e308, 0, 1e308], 'delta_unit': 'dB', 'max_increase': {'delta_db': 1e308, 'frequency_hz': 100}, 'min_change': {'delta_db': -1e308, 'frequency_hz': 0}}


def panel():
    p = ComparisonPanel.__new__(ComparisonPanel)
    p.app = SimpleNamespace(client=Mock(), session_id='session', selected_stream_id=lambda: 's', root=Mock(), snapshot=SimpleNamespace(session={'id': 'session'}, spectrum_stream_id='s', sweeps=[{'id': 'r', 'level_unit': 'dBm', 'interval': {}}, {'id': 't', 'level_unit': 'dBm'}]))
    p.box = Mock(); p.box.get.return_value = 'r'
    p.info = Mock(); p.button = Mock(); p.canvas = Mock()
    p.canvas.winfo_width.return_value = 500; p.canvas.winfo_height.return_value = 300
    p.scope = (id(p.app.client), 'session', 's')
    p.reference = 'r'; p.generation = 0; p.future = None; p.retired = None; p.closed = False; p.data = None
    p.executor = Mock(); p.executor.submit.return_value = Future()
    return p


class LifecycleTests(unittest.TestCase):
    def test_rolling_pin(self):
        p = panel(); p.update(); p.app.snapshot.sweeps = p.app.snapshot.sweeps[-1:]; p.update()
        self.assertEqual(p.reference, 'r')

    def test_success_and_overlap(self):
        p = panel(); p.compare(); p.compare(); p.executor.submit.assert_called_once()
        p.future.set_result(response()); p.finish(0)
        self.assertIsNotNone(p.data); p.canvas.create_line.assert_called_once()

    def test_errors(self):
        for message in ('404 sweep_not_found', '409 incompatible', 'network failure'):
            p = panel(); p.compare(); p.future.set_exception(RuntimeError(message)); p.finish(0)
            self.assertIn(message, p.info.set.call_args.args[0]); self.assertIsNone(p.data)

    def test_deadline_and_retry(self):
        p = panel(); p.compare(); old = p.future; old.set_running_or_notify_cancel()
        p.deadline = monotonic() - 1; p.finish(0); self.assertIsNone(p.future)
        p.compare(); self.assertEqual(p.executor.submit.call_count, 1)
        old.set_result(response()); p.executor.submit.return_value = Future(); p.compare()
        self.assertEqual(p.executor.submit.call_count, 2)

    def test_switches(self):
        for change in ('server', 'session', 'stream'):
            p = panel(); p.compare(); old = p.future
            if change == 'server': p.app.client = Mock()
            if change == 'session': p.app.session_id = 'new'
            if change == 'stream': p.app.selected_stream_id = lambda: 'new'
            p.finish(0); self.assertIsNone(p.reference); self.assertTrue(old.cancelled())

    def test_clear_pin_close(self):
        for action in ('clear', 'pin', 'close'):
            p = panel(); p.compare(); old = p.future; getattr(p, action)()
            self.assertTrue(old.cancelled()); p.finish(0); self.assertIsNone(p.data)

    def test_malformed(self):
        expected = ('session', 's', 'r', 't')
        for key, value in (('points', 2), ('session_id', 'wrong'), ('stream_id', 'wrong'), ('delta_unit', 'dBm'), ('delta_levels', [float('nan'), 0, 1]), ('frequencies_hz', [100, 0, 1])):
            data = response(); data[key] = value
            with self.assertRaises(ValueError): validate_comparison(data, expected)
        data = response(); data['target']['id'] = 'wrong'
        with self.assertRaises(ValueError): validate_comparison(data, expected)

    def test_plot_extremes_and_frequency_grid(self):
        for frequencies in ([0, 1, 100], [0, 0, 100], [1, 1, 1]):
            p = panel(); p.data = response(); p.data['frequencies_hz'] = frequencies; p.draw()
            points = p.canvas.create_line.call_args.args
            self.assertEqual(len(points), 6); self.assertTrue(all(isfinite(v) for v in points))
            if frequencies == [0, 1, 100]: self.assertAlmostEqual(points[2], 14.8)
            if frequencies == [1, 1, 1]: self.assertEqual(points[0::2], (250, 250, 250))


class ResponseParityTests(unittest.TestCase):
    def test_settings_json(self):
        expected = ('session', 's', 'r', 't')
        for settings in ({}, {'sequence': None}, {'rbw': None}, {'rbw': float('inf')}, {'rbw': {'nested': [None]}}, {'rbw': object()}):
            data = response(); data['reference']['settings'] = settings
            with self.assertRaises(ValueError): validate_comparison(data, expected)
        data = response(); data['reference']['settings']['sequence'] = {'nullable': [None]}
        validate_comparison(data, expected)

    def test_extrema_value_and_first_tie(self):
        expected = ('session', 's', 'r', 't')
        data = response(); data['max_increase']['delta_db'] = 20
        with self.assertRaises(ValueError): validate_comparison(data, expected)
        data = response(); data['delta_levels'] = [-1e308, 1e308, 1e308]
        with self.assertRaises(ValueError): validate_comparison(data, expected)
        data['max_increase']['frequency_hz'] = 1
        validate_comparison(data, expected)


class PinRetentionTests(unittest.TestCase):
    def test_pin_with_only_incompatible_latest(self):
        p = panel(); p.app.snapshot.sweeps = [{'id': 'new', 'level_unit': 's_meter_count'}]
        p.compare()
        p.executor.submit.assert_called_once_with(p.app.client.compare_sweeps, 'session', 'r', 'new')

    def test_persistent_pin_label(self):
        p = panel(); p.pin_label = Mock(); p.reference_interval = {'started_at': '2026-01-01T00:00:00Z'}
        p.app.snapshot.sweeps = p.app.snapshot.sweeps[-1:]; p.update()
        text = p.pin_label.set.call_args.args[0]
        self.assertIn('r', text); self.assertIn('UTC', text); self.assertIn('вне текущего окна', text)
        p.compare(); p.future.set_exception(RuntimeError('404')); p.finish(0)
        self.assertIn('вне текущего окна', p.pin_label.set.call_args.args[0])
