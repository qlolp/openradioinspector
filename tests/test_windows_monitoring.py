"""Synthetic Tk coverage for the thin monitoring receiver workspace."""
import io
import tempfile
import threading
import unittest
import wave
from unittest.mock import Mock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from radioinspector.windows.monitoring import MonitoringPanel


PROJECTS = {'items': [dict(id='p1', name='Room', site='Office', state='ready'),
                       dict(id='p2', name='Other', site='Roof', state='ready')]}
EVENT = dict(id='e1', project_id='p1', kind='new_signal', center_frequency_hz=100000000,
             last_seen='2026-11-10T12:00:00Z', state='open', disposition='unreviewed',
             analysis=dict(state='observed', audio=dict(session_id='s', stream_id='r', id='wav1')))


class Client:
    def __init__(self):
        self.calls = []
        self.result = {}

    def monitoring(self, path='', payload=None, *, query=None):
        self.calls.append((path, payload, query) if query is not None else (path, payload))
        return self.result

    def instrument_artifact(self, session, stream, identifier):
        self.calls.append((session, stream, identifier))
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
            wav.writeframes(b'\0\0' * 80)
        return buffer.getvalue()


class MonitoringTkTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tkinter import ttk
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        self.root.withdraw()
        self.pending = []
        self.app = SimpleNamespace(tk=tk, ttk=ttk, root=self.root, client=Client(), session_id='session',
                                   _closed=False, _revision=0)
        self.app._submit_action = lambda title, operation, callback: self.pending.append((operation, callback))
        self.panel = MonitoringPanel(self.app, ttk.Notebook(self.root))
        self.panel.refresh()
        self.complete(PROJECTS)

    def tearDown(self):
        if hasattr(self, 'root'):
            self.root.destroy()

    def complete(self, response=None):
        operation, callback = self.pending.pop(0)
        if response is not None:
            self.app.client.result = response
        result, errors = [], []
        def work():
            try: result.append(operation())
            except BaseException as exc: errors.append(exc)
        thread = threading.Thread(target=work)
        thread.start(); thread.join(3)
        self.assertFalse(thread.is_alive(), 'HTTP operation touched Tk or blocked')
        if errors: raise errors[0]
        callback(result[0])

    def test_event_items_never_replace_project_selector(self):
        self.panel.scoped('events')
        self.complete({'items': [EVENT]})
        self.assertEqual(self.panel.selected(), 'p1')
        self.assertEqual([p['id'] for p in self.panel.items], ['p1', 'p2'])
        self.assertEqual(len(self.panel.events), 1)
        self.assertIn('100000000', str(self.panel.event_tree.item('e1', 'values')))

    def test_late_project_and_endpoint_callbacks_do_not_render(self):
        self.panel.scoped('events')
        self.panel.choice.current(1); self.panel.project_changed()
        self.complete({'items': [EVENT]})
        self.assertEqual(self.panel.events, [])
        self.panel.refresh()
        self.app.client = Client()
        self.complete({'items': [dict(id='old', name='Stale', site='Old', state='ready')]})
        self.assertNotIn('Stale', self.panel.output.get('1.0', 'end'))
        self.panel.arm()
        self.assertEqual(self.pending, [])  # Previous endpoint's project is never armed.

    def test_audio_input_requires_explicit_selection_and_confirmation(self):
        self.panel.audio_inputs()
        self.complete(dict(available=True, devices=[dict(index=4, name='LISTEN', input_channels=1)], reason=None))
        self.assertEqual(self.panel.audio_choice.current(), -1)
        self.panel.configure_receiver()
        self.assertEqual(self.pending, [])
        self.panel.audio_choice.current(0)
        self.panel.configure_receiver()
        self.assertEqual(self.pending, [])
        self.panel.audio_confirm.set(True)
        self.panel.receiver_mode.set('FM')
        self.panel.configure_receiver(); self.complete({'id': 'p1', 'state': 'ready'})
        self.assertEqual(self.app.client.calls[-1], ('/p1/configure',
                         {'receiver_mode': 'FM', 'audio': {'device': 4, 'name': 'LISTEN', 'confirm': True}}))
        self.panel.disable_audio(); self.complete({'id': 'p1', 'state': 'ready'})
        self.assertEqual(self.app.client.calls[-1], ('/p1/configure', {'audio': None}))

    def test_arm_disarm_status_and_active_configuration_guard(self):
        self.panel.arm(); self.complete({'id': 'p1', 'state': 'armed'})
        self.assertEqual(self.app.client.calls[-1], ('/p1/arm', {}))
        self.panel.status(); self.complete(dict(project={'id': 'p1', 'state': 'armed'}, active=True,
                receiver_busy=True, error='Receiver error', workers=[], audio_configured=False, hardware_validation='not_run'))
        self.assertIn('Receiver error', self.panel.status_text.get())
        self.assertIn('не проверено', self.panel.status_text.get())
        self.panel.disable_audio()
        self.assertEqual(self.pending, [])
        self.panel.disarm(); self.complete({'id': 'p1', 'state': 'ready'})
        self.assertEqual(self.app.client.calls[-1], ('/p1/disarm', {}))

    def test_allowlist_watch_changes_is_explicit(self):
        self.panel.watch_changes.set(True)
        self.panel.allow_signal(); self.complete({})
        self.assertTrue(self.app.client.calls[-1][1]['watch_changes'])

    def select_event(self):
        self.panel.scoped('events'); self.complete({'items': [EVENT]})
        self.panel.event_tree.selection_set('e1')
        self.panel.event_detail(); self.complete(EVENT)
        self.assertEqual(self.app.client.calls[-1], ('/p1/events/e1', None))

    def test_saved_wav_download_and_non_windows_playback(self):
        self.select_event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'event.wav'
            with patch('tkinter.filedialog.asksaveasfilename', return_value=str(path)):
                self.panel.save_audio()
                self.complete()  # Download only: dialog and persistence follow a context check.
            if self.pending: self.complete()
            with wave.open(str(path)) as wav:
                self.assertEqual(wav.getnframes(), 80)
        with patch('radioinspector.windows.monitoring.sys.platform', 'linux'):
            self.panel.listen_audio()
        self.assertEqual(self.pending, [])
        self.assertIn('Windows', self.panel.output.get('1.0', 'end'))

    def test_event_change_discards_pending_wav(self):
        self.select_event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stale.wav'
            with patch('tkinter.filedialog.asksaveasfilename', return_value=str(path)):
                self.panel.save_audio()
            self.panel.choice.current(1); self.panel.project_changed()
            self.complete()
            self.assertFalse(path.exists())

    def test_windows_playback_downloads_on_executor_and_ignores_stale_event(self):
        self.select_event()
        winsound = SimpleNamespace(PlaySound=Mock(), SND_MEMORY=4, SND_NODEFAULT=2)
        with patch('radioinspector.windows.monitoring.sys.platform', 'win32'), patch.dict('sys.modules', winsound=winsound):
            self.panel.listen_audio()
            self.complete()  # Context validation before dispatching playback.
            winsound.PlaySound.assert_not_called()
            self.complete()
            raw, flags = winsound.PlaySound.call_args.args
            self.assertEqual(raw[:4], b'RIFF')
            self.assertEqual(flags, 6)
            winsound.PlaySound.reset_mock()
            self.panel.listen_audio()
            self.panel.event_tree.selection_remove('e1')
            self.complete()
            self.assertEqual(self.pending, [])
            winsound.PlaySound.assert_not_called()

    def test_unavailable_inputs_and_stale_save_dialog_are_safe(self):
        self.panel.audio_inputs()
        self.complete(dict(available=False, devices=[], reason='PortAudio unavailable'))
        self.assertIn('PortAudio unavailable', self.panel.audio_text.get())
        self.panel.configure_receiver()
        self.assertEqual(self.pending, [])
        self.select_event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'wrong-project.wav'
            def choose(**kwargs):
                self.panel.choice.current(1); self.panel.project_changed()
                return str(path)
            with patch('tkinter.filedialog.asksaveasfilename', side_effect=choose):
                self.panel.save_audio(); self.complete()
            self.assertEqual(self.pending, [])
            self.assertFalse(path.exists())

    def test_out_of_order_event_detail_and_non_event_items_do_not_corrupt_lists(self):
        self.select_event()
        self.panel.event_detail()
        self.panel.event_tree.selection_remove('e1')
        self.complete(dict(EVENT, analysis=dict(state='obsolete')))
        self.assertNotIn('obsolete', self.panel.output.get('1.0', 'end'))
        self.panel.preflight()
        self.complete(dict(ready=False, items=[dict(reason='missing receiver')]))
        self.assertEqual(self.panel.selected(), 'p1')
        self.assertEqual(len(self.panel.events), 1)

    def test_manual_roles_expiry_and_explicit_return(self):
        self.panel.manual('receiver', 'acquire')
        self.complete(dict(role='receiver', token='receiver-token', expires_seconds=30))
        self.panel.manual('analyzer', 'acquire')
        self.complete(dict(role='analyzer', token='analyzer-token', expires_seconds=30))
        self.panel.manual_frequency.set('145500001')
        self.panel.receiver_mode.set('FM')
        self.panel.manual_tune(); self.complete({})
        self.assertEqual(self.app.client.calls[-1], ('/p1/manual/tune',
            dict(token='receiver-token', frequency_hz=145500001, mode='FM')))
        self.panel.manual('receiver', 'renew'); self.complete(dict(role='receiver', expires_seconds=30))
        self.assertEqual(self.panel.leases['receiver']['token'], 'receiver-token')
        self.panel.leases['analyzer']['deadline'] = 0
        self.panel.start_search()
        self.assertEqual(self.pending, [])
        self.assertIn('истёк', self.panel.manual_text.get())
        self.panel.manual('analyzer', 'return'); self.complete(dict(role='analyzer', state='restoring'))
        self.assertNotIn('analyzer', self.panel.leases)
        self.assertIn('восстанавливается', self.panel.coverage_text.get())
        self.panel.choice.current(1); self.panel.project_changed()
        self.assertEqual(self.panel.leases, {})

    def test_search_uses_selected_event_and_core_series_and_marks(self):
        self.select_event()
        self.panel.manual('analyzer', 'acquire'); self.complete(dict(role='analyzer', token='a', expires_seconds=30))
        self.panel.search_span.set('200000')
        self.panel.start_search()
        self.complete(dict(active=True, coverage='narrow', event_id='e1', center_frequency_hz=100000000,
             maximum_level=-42, marks=[dict(label='У двери', created_at='2026-11-10T12:00:10Z', level=-44, frequency_hz=100000001)],
             series=[dict(sweep_id='s1', started_at='2026-11-10T12:00:00Z', ended_at='2026-11-10T12:00:01Z', peak_level=-80, frequency_hz=100000000),
                     dict(sweep_id='s2', started_at='2026-11-10T12:00:10Z', ended_at='2026-11-10T12:00:11Z', peak_level=-44, frequency_hz=100000001)]))
        self.assertEqual(self.app.client.calls[-1], ('/p1/search/start', dict(token='a', event_id='e1', span_hz=200000)))
        self.assertIn('-42', self.panel.coverage_text.get())
        self.assertIn('приостановлен', self.panel.coverage_text.get())
        self.assertTrue(self.panel.search_canvas.find_withtag('search-series'))
        self.assertIn('У двери', self.panel.marks_text.get())
        self.panel.position_label.set('Окно')
        self.panel.mark_position(); self.complete(dict(label='Окно', level=-50, frequency_hz=100000000))
        self.assertEqual(self.app.client.calls[-1], ('/p1/search/mark', dict(token='a', label='Окно')))
        self.panel.search_status(); self.complete(dict(active=False, coverage='paused', series=[], marks=[], maximum_level=None))
        self.assertIn('приостановлен', self.panel.coverage_text.get())

    def test_history_filters_pagination_utc_geometry_and_raw_comparison(self):
        self.panel.history_start.set('2026-11-10T12:00:00Z')
        self.panel.history_end.set('2026-11-10T12:10:00Z')
        self.panel.history_min.set('100')
        self.panel.history_max.set('300')
        self.panel.history_event.set('e1')
        self.panel.load_history()
        self.complete(history_page())
        self.assertEqual(self.app.client.calls[-1], ('/p1/history', None, dict(limit=64,
            start='2026-11-10T12:00:00Z', end='2026-11-10T12:10:00Z', min_hz=100, max_hz=300, event_id='e1')))
        self.assertEqual(len(self.panel.history_items), 3)
        canvas = self.panel.waterfall_canvas
        ys = [canvas.coords(canvas.find_withtag('sweep:' + key)[0])[1] for key in ('s1', 's2', 's3')]
        self.assertGreater(abs(ys[2]-ys[1]), abs(ys[1]-ys[0])*4)
        self.panel.history_choice.current(1); self.panel.history_selected()
        self.assertTrue(self.panel.spectrum_canvas.find_withtag('spectrum-current'))
        self.panel.raw_sweep(); self.complete(dict(id='s2', frequencies_hz=[100,200,300], levels=[-100,-50,-90], level_unit='dBm'))
        self.assertEqual(self.app.client.calls[-1], ('/p1/history/s2', None))
        self.panel.compare_sweep()
        self.complete(dict(baseline_id='b1', sweep_id='s2', detector_version='v1', compatible=True, display_only=True,
            items=[dict(frequency_hz=100, baseline_p50_dbm=-105, baseline_p95_dbm=-95, current_dbm=-100, delta_db=5),
                   dict(frequency_hz=300, baseline_p50_dbm=-110, baseline_p95_dbm=-100, current_dbm=-90, delta_db=20)]))
        self.assertTrue(self.panel.comparison_canvas.find_withtag('baseline-p50'))
        self.assertIn('v1', self.panel.comparison_text.get())
        self.panel.load_history(next_page=True); self.complete(dict(items=[], next_cursor=None))
        self.assertEqual(self.app.client.calls[-1][2]['after'], 'page+/=')
        self.assertEqual(self.panel.history_items, [])

    def test_history_and_manual_stale_callbacks_and_incompatible_baseline(self):
        self.panel.load_history()
        self.panel.history_min.set('123')
        self.complete(history_page())
        self.assertEqual(self.panel.history_items, [])
        self.panel.load_history(); self.complete(history_page())
        self.panel.compare_sweep()
        self.complete(dict(baseline_id='b', sweep_id='s1', detector_version='v1', compatible=False,
                           reason='Different grid', items=[], display_only=True))
        self.assertIn('Different grid', self.panel.comparison_text.get())
        self.assertFalse(self.panel.comparison_canvas.find_withtag('baseline-p50'))
        self.panel.manual('receiver', 'acquire')
        self.app.client = Client()
        self.complete(dict(role='receiver', token='stale', expires_seconds=30))
        self.assertEqual(self.panel.leases, {})


    def test_sources_page_and_stale_raw_sweep_are_scoped(self):
        self.panel.history_event.set('e1')
        self.panel.event_sources()
        self.complete(dict(items=[dict(id='ref1', event_id='e1', session_id='s', stream_id='a', sweep_id='s1')], next_cursor='ref1'))
        self.assertEqual(self.app.client.calls[-1], ('/p1/events/e1/sources', None, {'limit': 64}))
        self.assertEqual(self.panel.selected(), 'p1')
        self.panel.event_sources(next_page=True)
        self.complete(dict(items=[], next_cursor=None))
        self.assertEqual(self.app.client.calls[-1][2], {'limit': 64, 'after': 'ref1'})
        self.panel.load_history(); self.complete(history_page())
        self.panel.raw_sweep()
        self.panel.history_choice.current(1); self.panel.history_selected()
        self.complete(dict(id='s1', secret_old_detail='obsolete'))
        self.assertNotIn('obsolete', self.panel.output.get('1.0', 'end'))
        self.panel.manual('receiver', 'acquire'); self.complete(dict(token='r', role='receiver', expires_seconds=30))
        self.assertNotIn('"token"', self.panel.output.get('1.0', 'end'))
        self.panel.disarm(); self.complete(dict(id='p1', state='ready'))
        self.assertEqual(self.panel.leases, {})
        self.assertEqual(self.panel.history_event.get(), '')

    def test_invalid_history_filters_and_no_lease_do_not_issue_commands(self):
        self.panel.manual_tune()
        self.assertEqual(self.pending, [])
        self.panel.history_start.set('2026-11-10T12:00:00+03:00')
        self.panel.load_history()
        self.assertEqual(self.pending, [])
        self.panel.history_start.set('')
        self.panel.history_min.set('300'); self.panel.history_max.set('100')
        self.panel.load_history()
        self.assertEqual(self.pending, [])


    def test_small_viewport_can_scroll_to_history_charts(self):
        self.panel.notebook.pack(fill='both', expand=True)
        self.panel.workspace.select(self.panel.history_page)
        self.root.geometry('1040x300'); self.root.deiconify(); self.root.update()
        self.assertLess(self.panel.scroll_canvas.winfo_height(), self.panel.content.winfo_reqheight())
        self.panel.scroll_canvas.yview_moveto(1); self.root.update_idletasks()
        self.assertAlmostEqual(self.panel.scroll_canvas.yview()[1], 1, places=2)
        self.assertGreater(self.panel.waterfall_canvas.winfo_height(), 50)


def history_page():
    return dict(next_cursor='page+/=', items=[dict(id=key, session_id='s', stream_id='a',
        interval=dict(started_at=time, ended_at=time.replace('00Z','01Z').replace('10Z','11Z')),
        frequencies_hz=[100,200,300], levels=[-100,-50,-90], level_unit='dBm', settings={},
        original_point_count=1000, display_only=True)
        for key, time in [('s1','2026-11-10T12:00:00Z'), ('s2','2026-11-10T12:00:10Z'), ('s3','2026-11-10T12:01:10Z')]])


class MonitoringClientQueryTests(unittest.TestCase):
    def test_query_is_encoded_and_response_is_bounded(self):
        import json
        from urllib.parse import parse_qs, urlsplit
        from radioinspector.contracts import api_payload
        from radioinspector.windows.client import PlatformClient
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, count):
                self.count = count
                return json.dumps(api_payload(dict(items=[], next_cursor=None))).encode()
        class Opener:
            def open(self, request, timeout):
                self.request = request; self.response = Response()
                return self.response
        opener = Opener(); client = PlatformClient('http://example.test', opener=opener)
        client.monitoring('/p/history', query=dict(limit=64, after='page+/=', start='2026-11-10T12:00:00+00:00'))
        self.assertEqual(parse_qs(urlsplit(opener.request.full_url).query),
             dict(limit=['64'], after=['page+/='], start=['2026-11-10T12:00:00+00:00']))
        self.assertLessEqual(opener.response.count, 2_000_001)
        for path in ('/p/history?after=x', '/p/../history'):
            with self.assertRaises(ValueError): client.monitoring(path, query={})
