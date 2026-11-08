import io
import json
import os
import unittest
import urllib.error
from copy import deepcopy
from math import isfinite
from time import monotonic
from unittest.mock import Mock
from types import SimpleNamespace
from tests.test_windows_sweep_threshold import panel as base_panel
from radioinspector.contracts import api_payload
from radioinspector.windows.client import PlatformClient, PlatformClientError, MAX_RESPONSE_BYTES
from radioinspector.windows.sweep_three_thresholds import ThreeThresholdsPanel, validate_levels, validate_three_thresholds, result_text


LEVELS = [-90, -80, -70]


def source():
    return dict(id='a', stream_id='s', level_unit='dBm', frequencies_hz=[100, 200, 300], levels=[-95, -65, -95],
                settings={'rbw_hz': 1000, 'flag': True, 'sequence': {'index': 2, 'null': None}},
                interval={'started_at': '2026-11-08T00:00:00.000001Z', 'ended_at': '2026-11-08T00:00:01.000002Z'})


def response():
    s = source()
    return dict(session_id='session', stream_id='s', sweep={k: deepcopy(s[k]) for k in ('id', 'interval', 'levels', 'level_unit', 'settings')},
        frequencies_hz=s['frequencies_hz'], points=3, method='three_horizontal', comparison='strictly_greater', level_unit='dBm', delta_unit='dB',
        lines=[dict(id=f'line-{i}', level_dbm=level, excess_levels=[-95-level, -65-level, -95-level], exceedance_points=1, region_count=1,
                    regions=[dict(start_index=1, end_index=1, peak_index=1, start_frequency_hz=200, end_frequency_hz=200, peak_frequency_hz=200, points=1, peak_excess_db=-65-level)]) for i, level in enumerate(LEVELS, 1)])


def panel():
    base = base_panel(); p = ThreeThresholdsPanel.__new__(ThreeThresholdsPanel); p.__dict__.update(base.__dict__)
    p.level_vars = [Mock() for _ in LEVELS]
    for var, value in zip(p.level_vars, LEVELS): var.get.return_value = str(value)
    p.app.snapshot.sweeps = [source()]
    return p


class ValidationTests(unittest.TestCase):
    def check(self, data): return validate_three_thresholds(data, ('session', 's'), source(), LEVELS)

    def test_valid_and_independent_order(self):
        self.assertEqual(self.check(response()), response())
        text = result_text(response())
        for value in ('Session session', 'Stream s', 'Sweep a', '"sequence": {"index": 2, "null": null}', '"flag": true', '"rbw_hz": 1000', '.000001Z', '.000002Z'):
            self.assertIn(value, text)
        for levels in ([1, -2, 0], [-80]*3, [1e308, -1e308, 0]): validate_levels(levels)
        for levels in ([], [1, 2], [1]*4, [True, 1, 2], [float('nan'), 1, 2], [float('inf'), 1, 2]):
            with self.assertRaises(ValueError): validate_levels(levels)

    def test_adversarial_response(self):
        mutations = [lambda r: r.update(session_id='wrong'), lambda r: r.update(stream_id='wrong'), lambda r: r.update(points=True),
                     lambda r: r.update(method='rolling_median'), lambda r: r.update(comparison='greater_equal'), lambda r: r.update(level_unit='S'),
                     lambda r: r.update(delta_unit='dBm'), lambda r: r.update(frequencies_hz=[100., 200, 300]), lambda r: r.update(frequencies_hz=[100, 200, 301]),
                     lambda r: r['sweep']['settings'].update(flag=1), lambda r: r['sweep']['settings']['sequence'].update(index=3),
                     lambda r: r['sweep'].update(levels=[-95, -64, -95]), lambda r: r['lines'].reverse(), lambda r: r['lines'].pop()]
        for key, value in [('id', 'line-2'), ('level_dbm', -89), ('level_dbm', True), ('excess_levels', [0, 25, -5]),
                           ('excess_levels', [float('inf')]*3), ('region_count', True), ('exceedance_points', 2), ('regions', [])]:
            mutations.append(lambda r, k=key, v=value: r['lines'][0].update({k: v}))
        for key in response()['lines'][0]['regions'][0]:
            mutations.append(lambda r, k=key: r['lines'][0]['regions'][0].update({k: 999}))
        for mutate in mutations:
            r = response(); mutate(r)
            with self.subTest(r=r), self.assertRaises(ValueError): self.check(r)

    def test_utc_strict_source_and_echo(self):
        for time in ('0000-01-01T00:00:00Z', '2026-11-08T00:00:00.0000009Z', '2026-11-08 00:00:00Z', '2026-11-08T00:00:00+01:00', '2026-02-30T00:00:00Z', '2026-11-08T00:00:00Z\n'):
            s = source(); r = response(); s['interval']['started_at'] = r['sweep']['interval']['started_at'] = time
            with self.subTest(time=time), self.assertRaises(ValueError): validate_three_thresholds(r, ('session', 's'), s, LEVELS)
        r = response(); r['sweep']['interval']['started_at'] = '2026-11-08T00:00:00.000002Z'
        with self.assertRaises(ValueError): self.check(r)

    def test_first_peak_tie_and_equality(self):
        s = source(); s.update(levels=[-80]*3, frequencies_hz=[100]*3)
        r = response(); r.update(frequencies_hz=s['frequencies_hz']); r['sweep']['levels'] = s['levels']
        for i, line in enumerate(r['lines']):
            line.update(level_dbm=-90, excess_levels=[10]*3, exceedance_points=3, region_count=1,
                regions=[dict(start_index=0, end_index=2, peak_index=0, start_frequency_hz=100, end_frequency_hz=100, peak_frequency_hz=100, points=3, peak_excess_db=10)])
        validate_three_thresholds(r, ('session','s'), s, [-90]*3)
        r['lines'][0]['regions'][0]['peak_index'] = 1
        with self.assertRaises(ValueError): validate_three_thresholds(r, ('session','s'), s, [-90]*3)
        for line in r['lines']: line.update(level_dbm=-80, excess_levels=[0]*3, exceedance_points=0, region_count=0, regions=[])
        validate_three_thresholds(r, ('session','s'), s, [-80]*3)
        self.assertIn('совпадает', result_text(r))

    def test_text_first_200_per_line(self):
        r = response()
        for line in r['lines']: line['regions'] *= 250; line['region_count'] = 250
        text = result_text(r); self.assertEqual(text.count('Индексы'), 600); self.assertEqual(text.count('200 из 250'), 3)

    def test_round_trip_level_and_peak_display(self):
        r = response(); level = -90.0000001
        r['lines'][0]['level_dbm'] = level
        r['lines'][0]['excess_levels'] = [raw-level for raw in source()['levels']]
        peak = r['lines'][0]['excess_levels'][1]
        r['lines'][0]['regions'][0]['peak_excess_db'] = peak
        validate_three_thresholds(r, ('session', 's'), source(), [level, -80, -70])
        self.assertIn('line-1: -90.0000001 dBm', result_text(r))
        self.assertIn(f'пик +{peak} dB', result_text(r))
        p = panel(); p.data = r; p.draw()
        labels = [call.kwargs['text'] for call in p.canvas.create_text.call_args_list]
        self.assertIn('line-1: -90.0000001 dBm', labels)

    def test_one_bin_limits_overflow_and_unknown_source(self):
        s = source(); s.update(levels=[-80], frequencies_hz=[100])
        r = response(); r.update(points=1, frequencies_hz=[100]); r['sweep']['levels'] = [-80]
        for line in r['lines']: line.update(level_dbm=-80, excess_levels=[0], exceedance_points=0, region_count=0, regions=[])
        validate_three_thresholds(r, ('session','s'), s, [-80]*3)
        for key, value in [('frequencies_hz',[2**53]),('frequencies_hz',[True]),('levels',[True]),('settings',{'sequence':1}),('settings',{'rbw':None})]:
            broken = deepcopy(s); broken[key] = value
            with self.assertRaises(ValueError): validate_three_thresholds(r, ('session','s'), broken, [-80]*3)
        s['levels'] = [1e308]; r['sweep']['levels'] = [1e308]
        for line in r['lines']: line.update(level_dbm=-1e308, excess_levels=[1e308])
        with self.assertRaises(ValueError): validate_three_thresholds(r, ('session','s'), s, [-1e308]*3)


class LifecycleTests(unittest.TestCase):
    def test_explicit_frozen_single_flight(self):
        p = panel(); p.update(); p.executor.submit.assert_not_called(); p.analyze(); p.analyze()
        p.executor.submit.assert_called_once_with(p.app.client.sweep_three_thresholds, 'session', 'a', LEVELS)
        p.app.snapshot.sweeps[0]['settings']['sequence']['index'] = 99
        p.app.snapshot.sweeps = []; p.future.set_result(response()); p.finish(p.generation)
        self.assertEqual(p.data, response()); self.assertEqual(p.source['settings']['sequence']['index'], 2)
        p.update(); self.assertEqual(p.executor.submit.call_count, 1)

    def test_clear_edit_scope_disconnect_timeout_close_no_stale_queue(self):
        for action in ('clear', 'close', 'stream', 'session', 'server', 'disconnect', 'timeout'):
            p = panel(); p.analyze(); old = p.future; old.set_running_or_notify_cancel(); generation = p.generation
            if action in ('clear', 'close'): getattr(p, action)()
            if action == 'stream': p.app.selected_stream_id = lambda: 'other'
            if action == 'session': p.app.session_id = 'other'
            if action == 'server': p.app.client = Mock()
            if action == 'disconnect': p.app.client = None
            if action == 'timeout': p.deadline = monotonic()-1
            p.finish(generation); p.analyze(); self.assertEqual(p.executor.submit.call_count, 1)
            message = p.info.set.call_args; old.set_exception(RuntimeError('stale')); p.finish(generation)
            self.assertEqual(p.info.set.call_args, message); self.assertIsNone(p.data)

    def test_retired_finishes_then_explicit_retry(self):
        p = panel(); p.analyze(); old = p.future; old.set_running_or_notify_cancel(); p.clear(); p.analyze()
        self.assertEqual(p.executor.submit.call_count, 1)
        old.set_result(response()); p.analyze(); self.assertEqual(p.executor.submit.call_count, 2)
        p.future.set_result(response()); p.finish(p.generation); self.assertIsNotNone(p.data)

    def test_actual_refresh_failure_clears_running_and_completed(self):
        import queue
        from concurrent.futures import Future
        from radioinspector.windows.app import WindowsApp
        for running in (False, True):
            p = panel()
            if running:
                p.analyze(); old = p.future; old.set_running_or_notify_cancel(); generation = p.generation
            else: p.data = response()
            app = WindowsApp.__new__(WindowsApp)
            app._closed = False; app._revision = 7; app._messages = queue.Queue()
            app.root = Mock(); app.connection_var = Mock(); app._show_archive_capacity = Mock(); app._render_controls = Mock()
            app.three_thresholds_panel = p; app._refresh_running = True
            failed = Future(); failed.set_exception(PlatformClientError('offline',code='connection_error'))
            app._messages.put(('snapshot',failed,7,'')); app._drain_messages()
            self.assertIsNone(p.data)
            if running:
                old.set_result(response()); p.finish(generation); self.assertIsNone(p.data)

    def test_failures_do_not_publish(self):
        for status in (400, 404, 409, 413, 500):
            p = panel(); p.analyze(); p.future.set_exception(PlatformClientError('bad', status=status)); p.finish(p.generation)
            self.assertIsNone(p.data); p.analyze(); self.assertEqual(p.executor.submit.call_count, 2)

    def test_full_bins_extreme_finite_nested_coincident_markers(self):
        for n in (1, 3, 16384):
            p = panel(); p.data = response(); p.data.update(frequencies_hz=[100]*n, points=n)
            p.data['sweep']['levels'] = [-1e308]*n
            for line in p.data['lines']: line['level_dbm'] = -1e308
            p.draw(); self.assertEqual(p.canvas.create_rectangle.call_count, 3); p.canvas.create_oval.assert_called_once()
            calls = p.canvas.create_line.call_args_list
            self.assertEqual(len(calls), 3 + (n > 1))
            self.assertEqual(len({call.kwargs['dash'] for call in calls[:3]}), 3)
            if n > 1: self.assertEqual(len(calls[-1].args), n*2)
            for call in calls + p.canvas.create_rectangle.call_args_list + p.canvas.create_oval.call_args_list:
                self.assertTrue(all(isfinite(v) for v in call.args))
            sizes = [call.args[2]-call.args[0] for call in p.canvas.create_rectangle.call_args_list]
            self.assertEqual(sizes, [12,20,28])


class HTTPTests(unittest.TestCase):
    def test_exact_post_and_ids(self):
        c = PlatformClient('http://localhost'); c._request = Mock(); c.sweep_three_thresholds('s/a', '😀'*128, LEVELS)
        c._request.assert_called_once_with('POST', '/sessions/s%2Fa/sweep-three-thresholds', {'sweep_id':'😀'*128, 'levels_dbm':LEVELS}, expected_statuses=(200,))
        for value in (' ', '', '😀'*129, '\ud800', True):
            with self.assertRaises(ValueError): c.sweep_three_thresholds('s', value, LEVELS)

    def test_only_200_and_bounded_all_statuses_before_decode(self):
        for status in (200,201,202,204,206,400,404,409,413,500):
            for large in (False,True):
                raw = b'x'*(MAX_RESPONSE_BYTES+1) if large else json.dumps(api_payload(response())).encode()
                opener = Mock(); reply = Mock(); reply.__enter__ = Mock(return_value=reply); reply.__exit__ = Mock(return_value=False)
                reply.status = status; reply.read.return_value = raw
                if status >= 400: opener.open.side_effect = urllib.error.HTTPError('http://localhost',status,'error',{},io.BytesIO(raw))
                else: opener.open.return_value = reply
                c = PlatformClient('http://localhost',opener=opener)
                if status == 200 and not large: self.assertEqual(c.sweep_three_thresholds('session','a',LEVELS),response())
                else:
                    with self.assertRaises(PlatformClientError): c.sweep_three_thresholds('session','a',LEVELS)
                if status < 400: reply.read.assert_called_once_with(MAX_RESPONSE_BYTES+1)


@unittest.skipUnless(os.environ.get('THREE_THRESHOLDS_SMOKE_URL'), 'Explicit synthetic fixture and Tk desktop required')
class ActualTkHTTPTests(unittest.TestCase):
    def test_actual_panel_and_http(self):
        import tkinter as tk
        from tkinter import ttk
        from time import sleep
        root = tk.Tk(); root.withdraw()
        client = PlatformClient(os.environ['THREE_THRESHOLDS_SMOKE_URL'])
        app = SimpleNamespace(tk=tk, ttk=ttk, root=root, client=client, session_id='three-one', selected_stream_id=lambda:'spectrum-one',
            snapshot=SimpleNamespace(session={'id':'three-one'}, spectrum_stream_id='spectrum-one', sweeps=client.list_sweeps('three-one','spectrum-one')))
        notebook = ttk.Notebook(root); notebook.pack()
        p = ThreeThresholdsPanel(app, notebook)
        try:
            p.analyze()
            until = monotonic()+10
            while p.future is not None and monotonic() < until: root.update(); sleep(.02)
            self.assertIsNotNone(p.data, p.info.get())
            self.assertEqual(p.data['sweep']['id'], 'one-sweep-2')
            self.assertEqual([line['exceedance_points'] for line in p.data['lines']], [4,2,1])
            self.assertEqual([line['region_count'] for line in p.data['lines']], [2,2,1])
            self.assertEqual([line['regions'][0]['peak_excess_db'] for line in p.data['lines']], [25,15,5])
            line_items = [item for item in p.canvas.find_all() if p.canvas.type(item) == 'line']
            self.assertEqual(len(line_items), 4)
            self.assertEqual(len(p.canvas.coords(line_items[-1])), 202)
            p.level_vars[0].set('-80'); self.assertIsNone(p.data)
            p.analyze()
            until = monotonic()+10
            while p.future is not None and monotonic() < until: root.update(); sleep(.02)
            self.assertIsNotNone(p.data, p.info.get()); self.assertIn('совпадает', p.details.get('1.0','end'))
            self.assertEqual(sum(p.canvas.type(item)=='rectangle' for item in p.canvas.find_all()),3)
            app.client = None; p.update(); self.assertIsNone(p.data)
        finally:
            p.close(); root.destroy()
