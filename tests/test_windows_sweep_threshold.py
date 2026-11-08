import json
import unittest
from concurrent.futures import Future
from copy import deepcopy
from math import isfinite
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock
from radioinspector.contracts import api_payload
from radioinspector.windows.client import PlatformClient, PlatformClientError
from radioinspector.windows.sweep_threshold import ThresholdPanel, validate_threshold, validate_parameters, threshold_text


def response():
    return dict(session_id='session', stream_id='s', sweep=dict(id='a', level_unit='dBm', levels=[-50, -30, -50], settings={'rbw_hz': 1000}, interval={'started_at': '2026-11-08T00:00:00Z', 'ended_at': '2026-11-08T00:00:01Z'}), frequencies_hz=[100, 200, 300], parameters=dict(method='rolling_median', window_points=21, margin_db=6, edge_policy='clip', comparison='strictly_greater'), baseline_levels=[-50]*3, threshold_levels=[-44]*3, level_unit='dBm', excess_levels=[-6, 14, -6], delta_unit='dB', regions=[dict(start_index=1, end_index=1, peak_index=1, start_frequency_hz=200, end_frequency_hz=200, peak_frequency_hz=200, peak_excess_db=14, points=1)], points=3, exceedance_points=1, region_count=1)

EXPECTED = ('session', 's', 'a', 21, 6)


def panel():
    p = ThresholdPanel.__new__(ThresholdPanel)
    p.app = SimpleNamespace(client=Mock(), session_id='session', selected_stream_id=lambda: 's', root=Mock(), snapshot=SimpleNamespace(session={'id': 'session'}, spectrum_stream_id='s', sweeps=[dict(response()['sweep'], frequencies_hz=response()['frequencies_hz'], stream_id='s')]))
    p.scope = (id(p.app.client), 'session', 's'); p.generation = 0
    p.future = p.retired = p.data = None; p.closed = False
    p.executor = Mock(); p.executor.submit.side_effect = lambda *args: Future()
    for key in ('button', 'info', 'canvas', 'details', 'window', 'margin'): setattr(p, key, Mock())
    p.window.get.return_value = '21'; p.margin.get.return_value = '6'
    p.canvas.winfo_width.return_value = 500; p.canvas.winfo_height.return_value = 300
    return p


class ThresholdValidationTests(unittest.TestCase):
    def test_happy(self):
        self.assertEqual(validate_threshold(response(), EXPECTED), response())
        text = threshold_text(response())
        for term in ('UTC', 'Sweep: a', 'dB', '1/3', '200', '14'): self.assertIn(term, text)

    def test_frozen_source_fields(self):
        source = dict(response()['sweep'], frequencies_hz=response()['frequencies_hz'], stream_id='s')
        validate_threshold(response(), EXPECTED, source)
        for key, value in [('levels', [-51,-30,-50]), ('frequencies_hz', [1,2,3]), ('settings', {'rbw_hz':2000}), ('interval', {}), ('id', 'other')]:
            changed=deepcopy(source);changed[key]=value
            with self.assertRaises(ValueError): validate_threshold(response(), EXPECTED, changed)

    def test_large_region_text_is_bounded(self):
        data=response();data['regions'] *= 8192;data['region_count']=8192
        text=threshold_text(data)
        self.assertIn('200 из 8192',text)
        self.assertEqual(text.count('Индексы'),200)

    def test_params(self):
        for window, margin in [(2,6),(4,6),(103,6),(True,6),(21,float('nan')),(21,float('inf')),(21,-1),(21,121),(21,True)]:
            with self.assertRaises(ValueError): validate_parameters(window, margin)
        validate_parameters(101, 0)

    def test_malformed(self):
        changes = [('points', True), ('points', 2), ('session_id', 'wrong'), ('stream_id', 'wrong'), ('level_unit', 'S'), ('delta_unit', 'dBm'), ('threshold_levels', [-44]*2), ('excess_levels', [-6,float('nan'),-6]), ('frequencies_hz', [3,2,1]), ('frequencies_hz', [1,True,3]), ('region_count', 2), ('exceedance_points', 2), ('regions', [])]
        for key, value in changes:
            data = response(); data[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): validate_threshold(data, EXPECTED)
        for key, value in [('id','wrong'),('settings',{}),('settings',{'sequence':None}),('settings',{'rbw':None}),('levels',[-50,float('inf'),-50]),('interval',{'started_at':'2026-01-01','ended_at':'2026-01-02'})]:
            data=response();data['sweep'][key]=value
            with self.subTest(key=key), self.assertRaises(ValueError): validate_threshold(data, EXPECTED)
        for key, value in [('method','other'),('window_points',23),('margin_db',7),('edge_policy','pad'),('comparison','greater_equal')]:
            data=response();data['parameters'][key]=value
            with self.assertRaises(ValueError): validate_threshold(data, EXPECTED)
        for key in response()['regions'][0]:
            data=response();data['regions'][0][key] += 1
            with self.subTest(key=key), self.assertRaises(ValueError): validate_threshold(data, EXPECTED)

    def test_consistency_and_duplicates(self):
        for key in ('baseline_levels','threshold_levels','excess_levels'):
            data=response();data[key][0] += 1
            with self.assertRaises(ValueError): validate_threshold(data, EXPECTED)
        data=response();data['frequencies_hz']=[200]*3
        validate_threshold(data,EXPECTED)
        data['sweep']['settings']['sequence']={'nullable':[None]}; validate_threshold(data,EXPECTED)


class ThresholdLifecycleTests(unittest.TestCase):
    def test_explicit_single_flight_frozen_and_all_bins(self):
        p=panel();p.update();p.executor.submit.assert_not_called()
        p.analyze();p.analyze();p.executor.submit.assert_called_once_with(p.app.client.sweep_threshold,'session','a',21,6.0)
        p.app.snapshot.sweeps=[dict(id='new',level_unit='dBm')]
        p.future.set_result(response());p.finish(p.generation)
        self.assertEqual(p.data['sweep']['id'],'a');self.assertEqual(p.canvas.create_line.call_count,2)
        for call in p.canvas.create_line.call_args_list: self.assertEqual(len(call.args),6)
        p.update();self.assertEqual(p.executor.submit.call_count,1)

    def test_scope_disconnect_and_close_late(self):
        for action in ('server','session','stream','disconnect','clear','close'):
            p=panel();p.analyze();generation=p.generation;old=p.future;old.set_running_or_notify_cancel()
            if action=='server':p.app.client=Mock()
            if action=='session':p.app.session_id='other'
            if action=='stream':p.app.selected_stream_id=lambda:'other'
            if action=='disconnect':p.app.client=None
            if action in ('close','clear'):getattr(p,action)()
            p.finish(generation);old.set_result(response());p.finish(generation)
            self.assertIsNone(p.data)
            if action!='close':p.analyze()
            self.assertLessEqual(p.executor.submit.call_count,2)

    def test_timeout_no_queued_work(self):
        p=panel();p.analyze();generation=p.generation;old=p.future;old.set_running_or_notify_cancel()
        p.deadline=monotonic()-1;p.finish(generation);p.analyze()
        self.assertEqual(p.executor.submit.call_count,1)
        old.set_result(response());p.finish(generation);self.assertIsNone(p.data)
        p.analyze();self.assertEqual(p.executor.submit.call_count,2)

    def test_isolated_errors(self):
        for exc in (PlatformClientError('old',status=404),PlatformClientError('incompatible',status=409),RuntimeError('network')):
            p=panel();p.analyze();p.future.set_exception(exc);p.finish(p.generation)
            self.assertIsNone(p.data);self.assertIn('недоступен',p.info.set.call_args.args[0])
        p=panel();p.analyze();p.future.set_result({});p.finish(p.generation);self.assertIsNone(p.data)

    def test_invalid_params_no_request_and_wrong_snapshot(self):
        p=panel();p.window.get.return_value='4';p.analyze();p.executor.submit.assert_not_called()
        p=panel();p.app.snapshot.spectrum_stream_id='other';p.analyze();p.executor.submit.assert_not_called()

    def test_malformed_snapshot_source_is_not_requested(self):
        for changes in ({'stream_id': 'other'}, {'stream_id': None}, {'id': ''}, {'id': '  '}, {'id': 7}):
            p=panel();p.app.snapshot.sweeps[0].update(changes);p.analyze()
            p.executor.submit.assert_not_called()
        p=panel();p.app.snapshot.sweeps.append(dict(p.app.snapshot.sweeps[0], stream_id='other', id='cross-stream'))
        p.analyze()
        p.executor.submit.assert_called_once_with(p.app.client.sweep_threshold,'session','a',21,6.0)

    def test_drawing_finite_with_extreme_levels(self):
        p=panel();p.data=response();p.data['sweep']['levels']=[-1e308,0,1e308];p.data['threshold_levels']=[1e308]*3
        p.draw()
        for call in p.canvas.create_line.call_args_list:self.assertTrue(all(isfinite(v) for v in call.args))


class ThresholdHTTPTests(unittest.TestCase):
    def test_encoded_get(self):
        c=PlatformClient('http://localhost:8770');c._request=Mock(return_value=response())
        c.sweep_threshold('a/b','x&y',21,6)
        c._request.assert_called_once_with('GET','/sessions/a%2Fb/sweep-threshold?sweep_id=x%26y&window_points=21&margin_db=6',expected_statuses=(200,))

    def test_oversized_threshold_transport_is_rejected_without_truncation(self):
        from radioinspector.windows.client import MAX_RESPONSE_BYTES
        reply=Mock();reply.__enter__=Mock(return_value=reply);reply.__exit__=Mock(return_value=False)
        reply.status=200;reply.read.return_value=b'x' * (MAX_RESPONSE_BYTES + 1)
        opener=Mock();opener.open.return_value=reply
        client=PlatformClient('http://localhost',opener=opener)
        with self.assertRaises(PlatformClientError) as caught:
            client.sweep_threshold('session','a')
        self.assertEqual(caught.exception.code,'response_too_large')
        reply.read.assert_called_once_with(MAX_RESPONSE_BYTES+1)

    def test_http_200_only(self):
        for status in (200,201,202,204,206):
            reply=Mock();reply.__enter__=Mock(return_value=reply);reply.__exit__=Mock(return_value=False)
            reply.status=status;reply.read.return_value=json.dumps(api_payload(response())).encode()
            opener=Mock();opener.open.return_value=reply;c=PlatformClient('http://localhost',opener=opener)
            if status==200:self.assertEqual(c.sweep_threshold('session','a'),response())
            else:
                with self.assertRaises(PlatformClientError):c.sweep_threshold('session','a')
