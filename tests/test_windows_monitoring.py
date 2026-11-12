"""Synthetic Tk coverage for the thin monitoring receiver workspace."""
import io
import json
import sys
import tempfile
import threading
import time
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


class _ViewportDiagnostics:
    """Passive bounded evidence; never pumps events or suppresses body errors."""
    def __init__(self, root, targets, *, stream=None):
        self.root, self.targets = root, targets
        self.stream = sys.stderr if stream is None else stream
        self.started = time.monotonic()
        self.timer_ids, self.observer_ids = [], []
        self.counts = {name: dict(count=0, width=None, height=None) for name in targets}

    def _write(self, record):
        try:
            text = json.dumps(record, ensure_ascii=True, sort_keys=True)
            if len(text) > 8192:
                text = json.dumps(dict(phase=record['phase'], diagnostic_error='record_budget'))
            print(text, file=self.stream, flush=True)
        except Exception:
            # A broken diagnostic sink cannot replace an original assertion.
            pass

    def _error(self, phase, error):
        self._write(dict(phase=phase, diagnostic_error=type(error).__name__[:64]))

    def phase(self, phase):
        try:
            pending = self.root.tk.splitlist(self.root.tk.call('after', 'info'))
            timers = []
            for identifier in pending[:16]:
                info = self.root.tk.call('after', 'info', identifier)
                timers.append(dict(id=str(identifier)[:128], kind=str(info[1])[:16]))
            windows = {name: dict(width=widget.winfo_width(), height=widget.winfo_height(),
                                 requested_width=widget.winfo_reqwidth(),
                                 requested_height=widget.winfo_reqheight(),
                                 mapped=bool(widget.winfo_ismapped()))
                       for name, widget in self.targets.items()}
            self._write(dict(phase=phase, elapsed_seconds=round(time.monotonic()-self.started, 6),
                             tk_patchlevel=str(self.root.tk.call('package', 'provide', 'Tk'))[:64],
                             tcl_patchlevel=str(self.root.tk.call('info', 'patchlevel'))[:64],
                             windowing_system=str(self.root.tk.call('tk', 'windowingsystem'))[:16],
                             geometry=self.root.geometry()[:128], state=self.root.state()[:32],
                             windows=windows, configure=self.counts,
                             pending_after_count=len(pending), pending_after=timers))
        except Exception as error:
            self._error(phase, error)

    def __enter__(self):
        for name, widget in self.targets.items():
            def observed(event=None, name=name, widget=widget):
                # Toplevel bindtags also see descendant events. Count only the
                # named window, rather than misreporting child geometry as root.
                if event is not None and getattr(event, 'widget', None) is not widget:
                    return
                record = self.counts[name]
                record['count'] = min(record['count'] + 1, 1_000_000_000)
                for dimension in ('width', 'height'):
                    value = getattr(event, dimension, None)
                    if type(value) is int:
                        record[dimension] = value
            try:
                identifier = widget.bind('<Configure>', observed, add='+')
                self.observer_ids.append((widget, identifier))
            except Exception as error:
                self._error('bind-' + name, error)
        for delay in (30000, 60000):
            try:
                self.timer_ids.append(self.root.after(delay, lambda delay=delay: self.phase(f'after-{delay}ms')))
            except Exception as error:
                self._error('schedule', error)
        self.phase('installed')
        return self

    def __exit__(self, _kind, _error, _traceback):
        for identifier in self.timer_ids:
            try:
                self.root.after_cancel(identifier)
            except Exception as error:
                self._error('cancel-owned-timer', error)
        for widget, identifier in self.observer_ids:
            try:
                widget.unbind('<Configure>', identifier)
            except Exception as error:
                self._error('remove-owned-observer', error)
        return False


class Client:
    def __init__(self):
        self.calls = []
        self.result = {}
        self.responses = {}

    def monitoring(self, path='', payload=None, *, query=None):
        self.calls.append((path, payload, query) if query is not None else (path, payload))
        result = self.responses.get(path, self.result)
        if isinstance(result, Exception): raise result
        return result

    def monitoring_event_audio(self, project, identifier):
        self.calls.append(('event-audio', project, identifier))
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

    def test_disarm_and_manual_return_wait_for_foreground_action_without_being_lost(self):
        self.app._action_running=True
        self.panel.disarm()
        self.panel.leases['receiver']={'token':'owner','deadline':0}
        self.panel.manual('receiver','return')
        self.assertEqual(self.pending,[])
        self.app._action_running=False
        self.panel._drain_operator_actions()
        self.complete({'state':'ready'})
        self.panel._drain_operator_actions()
        self.complete({'state':'returned','role':'receiver'})
        self.assertIn(('/p1/disarm',{}),self.app.client.calls)
        self.assertIn(('/p1/manual/return',{'token':'owner'}),self.app.client.calls)

    def test_event_items_never_replace_project_selector(self):
        self.panel.scoped('events')
        self.complete({'items': [EVENT]})
        self.assertEqual(self.panel.selected(), 'p1')
        self.assertEqual([p['id'] for p in self.panel.items], ['p1', 'p2'])
        self.assertEqual(len(self.panel.events), 1)
        self.assertIn('100000000', str(self.panel.event_tree.item('e1', 'values')))

    def test_project_pages_preserve_selected_scope_and_use_cached_previous_cursors(self):
        self.panel.refresh()
        self.complete(dict(PROJECTS, next_cursor='page-one-last-id'))
        self.assertEqual(self.app.client.calls[-1], ('', None, {'limit': 200}))
        self.assertFalse(self.panel.project_previous.instate(['!disabled']))
        self.assertTrue(self.panel.project_next.instate(['!disabled']))
        generation = self.panel.generation
        self.panel.next_projects()
        self.complete({'items': [dict(id='p201', name='Later', site='Roof', state='ready')],
                       'next_cursor': 'page-two-last-id'})
        self.assertEqual(self.app.client.calls[-1], ('', None, {'limit': 200, 'after': 'page-one-last-id'}))
        self.assertEqual(self.panel.selected(), 'p1')
        self.assertEqual(self.panel.generation, generation)
        self.assertIn('Later', str(self.panel.choice['values']))
        self.panel.next_projects()
        self.complete({'items': [], 'next_cursor': None})
        self.assertEqual(self.app.client.calls[-1], ('', None, {'limit': 200, 'after': 'page-two-last-id'}))
        self.assertEqual(self.panel.selected(), 'p1')
        self.assertTrue(self.panel.project_next.instate(['disabled']))
        self.panel.previous_projects()
        self.complete({'items': [dict(id='p201', name='Later', site='Roof', state='ready')], 'next_cursor': 'page-two-last-id'})
        self.assertEqual(self.app.client.calls[-1], ('', None, {'limit': 200, 'after': 'page-one-last-id'}))
        self.panel.choice.current(next(i for i, project in enumerate(self.panel.items) if project['id'] == 'p201'))
        self.panel.project_changed()
        self.panel.arm(); self.complete({'state': 'armed'})
        self.assertEqual(self.app.client.calls[-1], ('/p201/arm', {}))
        self.panel.previous_projects(); self.complete(dict(PROJECTS, next_cursor='page-one-last-id'))
        self.assertEqual(self.app.client.calls[-1], ('', None, {'limit': 200}))
        self.assertEqual(self.panel.selected(), 'p201')

    def test_project_page_reply_cannot_restore_previous_endpoint_or_selection(self):
        self.panel.refresh(); self.complete(dict(PROJECTS, next_cursor='old-endpoint-cursor'))
        self.panel.next_projects()
        self.app.client = Client()
        self.panel.arm()
        self.complete({'items': [dict(id='old', name='Old endpoint', site='Roof', state='ready')], 'next_cursor': 'stale'})
        self.assertIsNone(self.panel.selected())
        self.assertEqual(self.pending, [])
        self.panel.refresh(); self.complete({'items': [], 'next_cursor': None})
        self.assertEqual(self.app.client.calls[-1], ('', None, {'limit': 200}))
        self.assertTrue(self.panel.project_previous.instate(['disabled']))
        self.assertTrue(self.panel.project_next.instate(['disabled']))

    def test_new_project_is_selected_without_assuming_it_appears_on_first_page(self):
        self.panel.create()
        self.complete(dict(id='random-uuid-outside-first-page', name='Created', site='Office', state='ready'))
        self.assertEqual(self.panel.selected(), 'random-uuid-outside-first-page')
        self.panel.refresh(); self.complete(dict(PROJECTS, next_cursor='more'))
        self.assertEqual(self.panel.selected(), 'random-uuid-outside-first-page')
        self.assertIn('Created', str(self.panel.choice['values']))
        self.panel.arm(); self.complete({'state': 'armed'})
        self.assertEqual(self.app.client.calls[-1], ('/random-uuid-outside-first-page/arm', {}))

    def test_project_paging_does_not_choose_a_project_after_selection_is_cleared(self):
        self.panel.refresh(); self.complete(dict(PROJECTS, next_cursor='more'))
        self.panel.choice.set(''); self.panel.project_changed()
        self.panel.next_projects()
        self.complete({'items': [dict(id='p201', name='Unselected', site='Roof', state='ready')], 'next_cursor': None})
        self.assertIsNone(self.panel.selected())
        self.panel.arm()
        self.assertEqual(self.pending, [])

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
                self.assertEqual(self.app.client.calls[-1], ('event-audio', 'p1', 'e1'))
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
        targets = dict(root=self.root, scroll=self.panel.scroll_canvas, content=self.panel.content,
                       history=self.panel.history_page, waterfall=self.panel.waterfall_canvas)
        with _ViewportDiagnostics(self.root, targets) as diagnostic:
            self.panel.notebook.pack(fill='both', expand=True)
            self.panel.workspace.select(self.panel.history_page)
            self.root.geometry('1040x300')
            diagnostic.phase('before-deiconify')
            self.root.deiconify()
            diagnostic.phase('after-deiconify')
            diagnostic.phase('before-update')
            self.root.update()
            diagnostic.phase('after-update')
            self.assertLess(self.panel.scroll_canvas.winfo_height(), self.panel.content.winfo_reqheight())
            self.panel.scroll_canvas.yview_moveto(1); self.root.update_idletasks()
            self.assertAlmostEqual(self.panel.scroll_canvas.yview()[1], 1, places=2)
            self.assertGreater(self.panel.waterfall_canvas.winfo_height(), 50)

    def test_viewport_diagnostics_remove_only_their_own_callbacks(self):
        probe = self.app.ttk.Frame(self.root)
        original_calls = []
        original_binding = probe.bind('<Configure>', lambda *_: original_calls.append(True))
        original_script = probe.bind('<Configure>')
        original_timer = self.root.after(90000, lambda: original_calls.append('timer'))
        existing_timers = set(self.root.tk.call('after', 'info'))
        output = io.StringIO()
        try:
            with self.assertRaisesRegex(AssertionError, 'original body assertion'):
                with _ViewportDiagnostics(self.root, {'probe': probe}, stream=output) as diagnostic:
                    owned_timers = set(diagnostic.timer_ids)
                    owned_observers = list(diagnostic.observer_ids)
                    self.assertEqual(len(owned_timers), 2)
                    self.assertEqual(set(self.root.tk.call('after', 'info')),
                                     existing_timers | owned_timers)
                    self.assertIn(original_script, probe.bind('<Configure>'))
                    self.assertEqual(len(owned_observers), 1)
                    # Exercise the actual registered Tcl/Python callbacks without
                    # draining or changing the native window's event queue.
                    self.root.tk.call(owned_observers[0][1])
                    self.root.tk.call(original_binding)
                    diagnostic.phase('helper-test')
                    self.assertEqual(diagnostic.counts['probe']['count'], 1)
                    self.assertEqual(original_calls, [True])
                    raise AssertionError('original body assertion')
            # Tkinter's funcid-only unbind can retain an empty separator line.
            # Every actual handler line and its order must remain byte-identical.
            self.assertEqual([line for line in probe.bind('<Configure>').splitlines() if line],
                             [line for line in original_script.splitlines() if line])
            self.assertEqual(set(self.root.tk.call('after', 'info')), existing_timers)
            for _widget, identifier in owned_observers:
                self.assertFalse(self.root.tk.call('info', 'commands', identifier))
            self.root.tk.call(original_binding)
            self.assertEqual(original_calls, [True, True])
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            record = next(item for item in records if item['phase'] == 'helper-test')
            self.assertEqual(record['configure']['probe']['count'], 1)
            self.assertTrue(record['tk_patchlevel'])
            self.assertIn(record['windowing_system'], ('x11', 'aqua', 'win32'))
            self.assertLess(len(output.getvalue()), 8192)
            print(output.getvalue(), file=sys.stderr, end='', flush=True)
            print(json.dumps(dict(phase='helper-ownership',
                                  original_callback_calls=len(original_calls),
                                  original_handler_lines_preserved=True,
                                  original_timer=original_timer,
                                  owned_timer_ids=sorted(owned_timers),
                                  remaining_after_ids=sorted(existing_timers),
                                  owned_observer_commands_removed=True,
                                  original_assertion_propagated=True)),
                  file=sys.stderr, flush=True)
        finally:
            self.root.after_cancel(original_timer)
            probe.unbind('<Configure>', original_binding)
            probe.destroy()


    def test_settings_are_explicit_and_secret_is_not_rendered(self):
        self.assertFalse(self.panel.autostart.get())
        self.assertFalse(self.panel.beep_enabled.get())
        self.panel.save_settings()
        self.assertEqual(self.pending, [])
        self.panel.load_settings(); self.complete(settings())
        self.assertEqual(self.panel.minimum_free.get(), '268435456')
        self.assertEqual(self.panel.minimum_retention.get(), '3600')
        self.panel.autostart.set(True)
        self.panel.webhook_enabled.set(True)
        self.panel.webhook_url.set('https://example.test/hook')
        self.panel.webhook_secret.set('TOP-SECRET-VALUE')
        self.panel.save_settings()
        self.assertEqual(self.panel.webhook_secret.get(), '')
        self.complete(dict(settings(), webhook=dict(enabled=True, url='https://example.test/hook', secret_configured=True, secret='TOP-SECRET-VALUE')))
        path, payload = self.app.client.calls[-1]
        self.assertEqual(path, '/p1/settings')
        self.assertTrue(payload['autostart'])
        self.assertEqual(payload['webhook']['secret'], 'TOP-SECRET-VALUE')
        self.assertNotIn('TOP-SECRET-VALUE', self.panel.output.get('1.0', 'end'))
        self.panel.save_settings(); self.complete(settings())
        self.assertNotIn('secret', self.app.client.calls[-1][1]['webhook'])

    def test_secret_is_redacted_from_transport_errors_and_busy_save_keeps_entry(self):
        self.panel.load_settings(); self.complete(settings())
        self.panel.webhook_secret.set('PRIVATE-123')
        self.app._action_running = True
        self.panel.save_settings()
        self.assertEqual(self.pending, [])
        self.assertEqual(self.panel.webhook_secret.get(), 'PRIVATE-123')
        self.app._action_running = False
        self.app.client.responses['/p1/settings'] = RuntimeError('failed using PRIVATE-123')
        self.panel.save_settings()
        with self.assertRaises(Exception) as caught: self.complete()
        self.assertNotIn('PRIVATE-123', str(caught.exception))
        self.assertEqual(self.panel.webhook_secret.get(), '')

    def test_storage_unknown_forecast_and_readiness_are_human_readable(self):
        self.panel.storage()
        self.complete(storage())
        self.assertIn('неизвест', self.panel.storage_text.get())
        self.assertIn('выключено', self.panel.storage_text.get())
        self.assertIn('не готов', self.panel.storage_text.get())
        self.panel.storage()
        self.complete(dict(storage(), ready=True, bytes_per_second=1024, estimated_retention_seconds=7200))
        self.assertIn('2.0 ч', self.panel.storage_text.get())
        self.assertIn('готов', self.panel.storage_text.get())

    def test_alert_initial_snapshot_is_quiet_and_new_unacknowledged_alert_beeps_once(self):
        self.panel.beep_enabled.set(True)
        with patch.object(self.root, 'bell') as bell:
            self.panel.load_alerts(); self.complete(dict(items=[alert('a1')], next_cursor='older+/='))
            bell.assert_not_called()
            self.panel.load_alerts(); self.complete(dict(items=[alert('a2'), alert('a1')], next_cursor=None))
            bell.assert_called_once()
            self.panel.load_alerts(); self.complete(dict(items=[alert('a2'), alert('a1'), alert('a3', acknowledged_at='2026-11-10T12:00:00Z')], next_cursor=None))
            bell.assert_called_once()
        self.panel.alert_tree.selection_set('a2'); self.panel.alert_selected()
        self.assertIn('отключена', self.panel.alert_text.get())
        self.assertIn('0', self.panel.alert_text.get())

    def test_alert_ack_and_event_verdict_keep_project_context(self):
        self.panel.load_alerts(); self.complete(dict(items=[alert('a1')], next_cursor=None))
        self.panel.alert_tree.selection_set('a1')
        self.panel.ack_alert(); self.complete(dict(id='a1',acknowledged=True))
        self.assertEqual(self.app.client.calls[-1], ('/p1/alerts/a1/ack', {}))
        self.assertIn('подтверждено', str(self.panel.alert_tree.item('a1', 'values')))
        self.select_event()
        self.panel.verdict.set('Ложное срабатывание')
        self.panel.verdict_comment.set('Проверено оператором')
        self.panel.review_event(); self.complete(dict(EVENT, disposition='false_positive', operator_comment='Проверено оператором'))
        self.assertEqual(self.app.client.calls[-1], ('/p1/events/e1/review',
            dict(disposition='false_positive', comment='Проверено оператором')))
        self.assertEqual(self.panel.selected(), 'p1')
        self.assertIn('Ложное срабатывание', str(self.panel.event_tree.item('e1', 'values')))

    def test_read_only_poll_runs_when_other_tab_selected_and_does_not_overwrite_input(self):
        self.panel.receiver_mode.set('FM')
        self.panel.workspace.select(self.panel.history_page)
        self.panel.render({'retained': 'Operator diagnostic'})
        self.app.client.responses.update({'/p1/alerts': dict(items=[alert('a1')], next_cursor=None),
            '/p1/storage': storage(), '/p1/status': dict(active=True, project=dict(id='p1',state='armed',receiver_mode='AM'),
                 receiver_busy=False, workers=[], error=None, audio_configured=False, hardware_validation='not_run')})
        self.app.client.calls.clear()
        self.panel.poll_operations(); self.complete()
        self.assertEqual({call[0] for call in self.app.client.calls}, {'/p1/alerts','/p1/storage','/p1/status'})
        self.assertTrue(all(call[1] is None for call in self.app.client.calls))
        self.assertEqual(self.panel.receiver_mode.get(), 'FM')
        self.assertIn('Operator diagnostic', self.panel.output.get('1.0','end'))
        self.assertIn('a1', self.panel.alert_tree.get_children())
        self.assertEqual(set(self.panel._tk_timers.pending), {'operations'})

    def test_poll_busy_disconnect_stale_completion_and_close_cancel_timer(self):
        self.app._action_running = True
        self.panel.poll_operations()
        self.assertEqual(self.pending, [])
        self.assertEqual(len(self.panel._tk_timers.pending), 1)
        self.app._action_running = False
        self.panel.poll_operations()
        self.panel.choice.current(1); self.panel.project_changed()
        self.app.client.responses.update({'/p1/alerts': dict(items=[alert('stale')], next_cursor=None), '/p1/storage': storage()})
        self.complete()
        self.assertNotIn('stale', self.panel.alert_tree.get_children())
        self.app.client = None
        self.panel.poll_operations()
        self.assertEqual(self.panel._tk_timers.pending, {})
        self.panel.close()
        self.assertTrue(self.panel.closed)
        self.assertEqual(self.panel._tk_timers.pending, {})

    def test_alert_pagination_preserves_older_page_during_background_updates(self):
        self.panel.load_alerts(); self.complete(dict(items=[alert('a1')], next_cursor='older+/='))
        self.panel.load_alerts(next_page=True); self.complete(dict(items=[alert('old')], next_cursor=None))
        self.assertEqual(self.app.client.calls[-1], ('/p1/alerts', None, {'limit':50, 'after':'older+/='}))
        self.app.client.responses['/p1/alerts'] = dict(items=[alert('a2')], next_cursor=None)
        self.panel.poll_operations(); self.complete()
        self.assertEqual(self.panel.alert_tree.get_children(), ('old',))
        self.assertIn('a2', self.panel.seen_alerts)


    def test_new_guard_session_follows_live_once_and_preserves_project_and_leases(self):
        followed = []
        def use_session(data):
            followed.append(data['id'])
            self.app.session_id = data['id']
            self.app._revision += 1
        self.app._use_created_session = use_session
        self.app.client.get_session = lambda sid: dict(id=sid,state='running',streams=[])
        self.panel.leases['receiver'] = dict(token='retained', deadline=10**20)
        self.panel.arm(); self.complete(dict(id='p1',state='armed',active_session_id='guard-1'))
        self.complete()
        self.assertEqual(followed, ['guard-1'])
        self.assertEqual(self.panel.selected(), 'p1')
        self.panel._sync_context()
        self.assertEqual(self.panel.leases['receiver']['token'], 'retained')
        self.app.session_id = 'manual-history'; self.app._revision += 1
        self.panel.status(); self.complete(dict(active=True, project=dict(id='p1',state='armed',active_session_id='guard-1'), workers=[]))
        self.assertEqual(self.pending, [])
        self.assertEqual(self.app.session_id, 'manual-history')
        self.panel.status(); self.complete(dict(active=True, project=dict(id='p1',state='armed',active_session_id='guard-2'),workers=[]))
        self.complete()
        self.assertEqual(followed, ['guard-1','guard-2'])


    def test_preparation_saves_unready_state_and_requires_saved_serial_confirmations(self):
        self.assertFalse(self.panel.receive_path_confirmed.get())
        self.assertFalse(self.panel.location_confirmed.get())
        self.panel.antenna_notes.set('Антенна у окна; тракт tinySA/AR-DV1 проверен')
        self.panel.save_preparation(); self.complete(prepared_project(confirmed=False))
        self.assertEqual(self.app.client.calls[-1], ('/p1/preparation', dict(
            antenna_notes='Антенна у окна; тракт tinySA/AR-DV1 проверен',receive_path_confirmed=False,location_confirmed=False)))
        self.assertEqual(str(self.panel.preparation_start_button.cget('state')), 'disabled')
        self.panel.start_preparation()
        self.assertEqual(self.pending, [])
        self.panel.receive_path_confirmed.set(True); self.panel.location_confirmed.set(True)
        self.panel.save_preparation(); self.complete(prepared_project())
        self.assertEqual(str(self.panel.preparation_start_button.cget('state')), 'normal')
        self.panel.start_preparation(); self.complete(dict(project=dict(prepared_project(),state='collecting',coverage='preparation'),workers=[dict(state='running',stream_id='a')]))
        self.assertEqual(self.app.client.calls[-1], ('/p1/preparation/start', {}))
        self.assertTrue(self.panel.collecting_baseline)
        self.assertIn('аппарат',self.panel.preparation_text.get())
        self.panel.receive_path_confirmed.set(False)
        self.assertEqual(str(self.panel.preparation_start_button.cget('state')), 'disabled')

    def test_preparation_load_validation_and_stale_project_response(self):
        self.panel.save_preparation()
        self.assertEqual(self.pending, [])
        self.panel.antenna_notes.set('x'*1001); self.panel.save_preparation()
        self.assertEqual(self.pending, [])
        self.panel.load_preparation(); self.complete(prepared_project())
        self.assertEqual(self.app.client.calls[-1], ('/p1',None))
        self.assertEqual(self.panel.antenna_notes.get(),'Приёмная антенна; кабель проверен')
        self.assertTrue(self.panel.receive_path_confirmed.get())
        self.panel.load_preparation()
        self.panel.choice.current(1); self.panel.project_changed()
        self.complete(prepared_project())
        self.assertEqual(self.panel.antenna_notes.get(),'')
        self.assertFalse(self.panel.receive_path_confirmed.get())

    def test_baseline_progress_requires_core_ready_and_explicit_operator_confirmation(self):
        self.panel.preflight(); self.complete(collection_progress(20,59.5,False))
        self.assertIn('20 / 20',self.panel.collection_text.get())
        self.assertIn('59.5 / 60',self.panel.collection_text.get())
        self.panel.confirm.set(True)
        self.panel.baseline()
        self.assertEqual(self.pending, [])
        self.panel.preflight(); self.complete(collection_progress(25,63,True))
        self.panel.confirm.set(False); self.panel.baseline()
        self.assertEqual(self.pending, [])
        self.panel.confirm.set(True); self.panel.baseline()
        self.complete(dict(id='b1',confirmed=True))
        self.assertEqual(self.app.client.calls[-1], ('/p1/baselines', dict(name='Наблюдение',location='Объект',confirm=True)))
        self.assertFalse(self.panel.collecting_baseline)

    def test_collection_poll_is_read_only_and_preserves_preparation_and_detector_edits(self):
        self.panel.load_preparation(); self.complete(prepared_project())
        self.panel.load_detector(); self.complete(dict(settings(),detector=detector_parameters()))
        self.panel.antenna_notes.set('Новая несохранённая заметка')
        self.panel.detector_values['margin_db'].set('9')
        self.panel.collecting_baseline = True
        self.app.client.responses.update({'/p1/alerts':dict(items=[],next_cursor=None),'/p1/storage':storage(),
            '/p1/status':dict(active=False,project=dict(prepared_project(),state='collecting',coverage='preparation'),workers=[],hardware_validation='not_run'),
            '/p1/preflight':collection_progress(19,58,False)})
        self.app.client.calls.clear()
        self.panel.poll_operations(); self.complete()
        self.assertIn('/p1/preflight',[call[0] for call in self.app.client.calls])
        self.assertTrue(all(call[1] is None for call in self.app.client.calls))
        self.assertEqual(self.panel.antenna_notes.get(),'Новая несохранённая заметка')
        self.assertEqual(self.panel.detector_values['margin_db'].get(),'9')
        self.assertIn('19 / 20',self.panel.collection_text.get())

    def test_detector_controls_send_only_parameters_and_preserve_integer_counts(self):
        self.panel.load_detector(); self.complete(dict(settings(),detector=detector_parameters()))
        self.panel.webhook_secret.set('UNSAVED-SECRET')
        self.panel.detector_values['margin_db'].set('9')
        self.panel.balanced_detector()
        self.assertEqual(self.pending, [])
        self.panel.save_detector(); self.complete(dict(settings(),detector=detector_parameters()))
        self.assertEqual(self.app.client.calls[-1], ('/p1/settings', {'detector':detector_parameters()}))
        parameters=self.app.client.calls[-1][1]['detector']
        self.assertIs(type(parameters['confirmations']),int)
        self.assertIs(type(parameters['window_sweeps']),int)
        self.assertEqual(self.panel.webhook_secret.get(),'UNSAVED-SECRET')
        self.assertIn('robust-city-v1',self.panel.detector_text.get())
        self.assertIn('не проверено',self.panel.detector_text.get())

    def test_detector_validation_and_active_guard_do_not_submit(self):
        self.panel.load_detector(); self.complete(dict(settings(),detector=detector_parameters()))
        for key,value in [('margin_db','nan'),('confirmations','3.5'),('confirmations','6'),('hysteresis_db','7')]:
            self.panel.balanced_detector(); self.panel.detector_values[key].set(value)
            self.panel.save_detector()
            self.assertEqual(self.pending, [])
        self.panel.balanced_detector(); self.panel.active=True
        self.panel.save_detector()
        self.assertEqual(self.pending, [])
        self.assertIn('снимите',self.panel.status_text.get())


    def test_collection_new_session_follows_once_and_stop_is_explicit(self):
        followed=[]
        def use_session(data):
            followed.append(data['id']); self.app.session_id=data['id']; self.app._revision+=1
        self.app._use_created_session=use_session
        self.app.client.get_session=lambda sid:dict(id=sid,state='running',streams=[])
        self.panel.load_preparation(); self.complete(prepared_project())
        self.panel.start_preparation()
        collecting=dict(prepared_project(),active_session_id='collection-new',state='collecting',coverage='preparation')
        self.complete(dict(project=collecting,workers=[])); self.complete()
        self.assertEqual(followed,['collection-new'])
        self.app.session_id='manual-history'; self.app._revision+=1
        self.panel.status(); self.complete(dict(active=False,project=collecting,workers=[]))
        self.assertEqual(self.pending,[])
        self.assertEqual(self.app.session_id,'manual-history')
        self.assertEqual(str(self.panel.preparation_stop_button.cget('state')),'normal')
        self.assertEqual(str(self.panel.preparation_start_button.cget('state')),'disabled')
        self.panel.preflight(); self.complete(collection_progress(30,65,True))
        self.panel.confirm.set(True); self.panel.baseline(); self.complete(dict(id='b1',confirmed=True))
        self.assertTrue(self.panel.collecting_baseline)  # Core owns capture until stop/arm.
        self.panel.stop_preparation(); self.complete(dict(project=dict(collecting,state='ready',coverage='stopped'),workers=[]))
        self.assertEqual(self.app.client.calls[-1],('/p1/preparation/stop',{}))
        self.assertFalse(self.panel.collecting_baseline)
        self.assertEqual(str(self.panel.preparation_stop_button.cget('state')),'disabled')
        self.assertEqual(self.panel.selected(),'p1')

    def test_explicit_simulator_profile_bypasses_physical_declarations(self):
        simulated=dict(prepared_project(False),profile=dict(analyzer=dict(settings={'adapter':'tinysa-sim'}),receiver=dict(settings={'adapter':'ardv1-sim'})),preparation={})
        self.panel.load_preparation(); self.complete(simulated)
        self.assertFalse(self.panel.receive_path_confirmed.get())
        self.assertFalse(self.panel.location_confirmed.get())
        self.assertIn('Симуляция',self.panel.preparation_text.get())
        self.assertEqual(str(self.panel.preparation_start_button.cget('state')),'normal')
        self.panel.start_preparation(); self.complete(dict(project=dict(simulated,state='collecting',coverage='preparation'),workers=[]))
        self.assertEqual(self.app.client.calls[-1],('/p1/preparation/start',{}))


    def test_degraded_guard_labels_coverage_and_keeps_configuration_locked(self):
        self.panel.status(); self.complete(dict(active=True,receiver_busy=False,error='AR-DV1 disconnected',
            audio_configured=False,hardware_validation='not_run',project=dict(id='p1',state='degraded',coverage='wide'),
            workers=[dict(stream_id='a',state='running',alive=True),dict(stream_id='r',state='error',alive=False)]))
        self.assertIn('отказ',self.panel.status_text.get())
        self.assertIn('широкий обзор',self.panel.status_text.get())
        self.assertIn('AR-DV1 disconnected',self.panel.status_text.get())
        self.panel.disable_audio()
        self.assertEqual(self.pending,[])
        self.panel.status(); self.complete(dict(active=True,workers=[],error='tinySA disconnected',
            hardware_validation='not_run',project=dict(id='p1',state='degraded',coverage='unavailable')))
        self.assertIn('недоступен',self.panel.status_text.get())
        self.panel.search_status(); self.complete(dict(active=False,coverage='unavailable',series=[],marks=[],maximum_level=None))
        self.assertIn('недоступен',self.panel.coverage_text.get())


    def test_loaded_active_project_cannot_start_collection(self):
        self.panel.load_preparation(); self.complete(dict(prepared_project(),state='degraded'))
        self.assertEqual(str(self.panel.preparation_start_button.cget('state')),'disabled')
        self.panel.start_preparation()
        self.assertEqual(self.pending,[])


    def test_active_search_poll_updates_core_chart_without_commands_or_lease_renewal(self):
        self.panel.search_data=dict(active=True,coverage='narrow',series=[],marks=[],maximum_level=None)
        self.panel.leases['analyzer']=dict(token='expired-token',deadline=0)
        self.panel.next_health_poll=10**20
        self.panel.render({'retained':'Operator data'})
        self.app.client.responses.update({'/p1/alerts':dict(items=[],next_cursor=None),'/p1/search/status':
            dict(active=True,coverage='narrow',maximum_level=-25,event_id='e1',
                series=[dict(sweep_id='s1',started_at='2026-11-10T12:00:00Z',ended_at='2026-11-10T12:00:01Z',peak_level=-80,frequency_hz=100000000),
                        dict(sweep_id='s2',started_at='2026-11-10T12:00:05Z',ended_at='2026-11-10T12:00:06Z',peak_level=-30,frequency_hz=100000001)],
                marks=[dict(label='Новый пункт',created_at='2026-11-10T12:00:05Z',level=-30,frequency_hz=100000001)])})
        self.app.client.calls.clear()
        self.panel.poll_operations(); self.complete()
        self.assertEqual({call[0] for call in self.app.client.calls},{'/p1/alerts','/p1/search/status'})
        self.assertTrue(all(call[1] is None for call in self.app.client.calls))
        self.assertTrue(self.panel.search_canvas.find_withtag('search-series'))
        self.assertEqual(len(self.panel.search_data['series']),2)
        self.assertIn('-25',self.panel.coverage_text.get())
        self.assertIn('Новый пункт',self.panel.marks_text.get())
        self.assertEqual(self.panel.leases['analyzer']['deadline'],0)
        self.assertIn('Operator data',self.panel.output.get('1.0','end'))

    def test_search_poll_skips_inactive_and_tracks_explicit_restoration_until_wide(self):
        self.panel.next_health_poll=10**20
        self.app.client.responses['/p1/alerts']=dict(items=[],next_cursor=None)
        self.app.client.calls.clear()
        self.panel.poll_operations(); self.complete()
        self.assertNotIn('/p1/search/status',[call[0] for call in self.app.client.calls])
        self.panel.leases['analyzer']=dict(token='expired-token',deadline=0)
        self.panel.manual('analyzer','return'); self.complete(dict(role='analyzer',state='restoring'))
        self.app.client.responses['/p1/search/status']=dict(active=False,coverage='restoring',series=[],marks=[],maximum_level=None)
        self.app.client.calls.clear()
        self.panel.poll_operations(); self.complete()
        self.assertIn('/p1/search/status',[call[0] for call in self.app.client.calls])
        self.app.client.responses['/p1/search/status']=dict(active=False,coverage='wide',series=[],marks=[],maximum_level=None)
        self.panel.poll_operations(); self.complete()
        self.app.client.calls.clear()
        self.panel.poll_operations(); self.complete()
        self.assertNotIn('/p1/search/status',[call[0] for call in self.app.client.calls])
        self.assertNotIn('analyzer',self.panel.leases)


def history_page():
    return dict(next_cursor='page+/=', items=[dict(id=key, session_id='s', stream_id='a',
        interval=dict(started_at=time, ended_at=time.replace('00Z','01Z').replace('10Z','11Z')),
        frequencies_hz=[100,200,300], levels=[-100,-50,-90], level_unit='dBm', settings={},
        original_point_count=1000, display_only=True)
        for key, time in [('s1','2026-11-10T12:00:00Z'), ('s2','2026-11-10T12:00:10Z'), ('s3','2026-11-10T12:01:10Z')]])


class MonitoringClientQueryTests(unittest.TestCase):
    def test_receiver_wav_download_is_bounded(self):
        from radioinspector.windows.client import PlatformClient
        response=Mock()
        response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        response.read.return_value=b'x'*(8*1024*1024+1)
        opener=Mock();opener.open.return_value=response
        client=PlatformClient('http://example.test',opener=opener)
        with self.assertRaisesRegex(ValueError,'too large'):client.monitoring_event_audio('p','e')
        response.read.assert_called_once_with(8*1024*1024+1)
        self.assertEqual(opener.open.call_args.args[0].full_url,
                         'http://example.test/api/v1/monitoring-projects/p/events/e/audio')

    def test_receiver_wav_download_uses_project_event_http_route(self):
        import base64
        import hashlib
        from radioinspector.web.server import create_server
        from radioinspector.windows.client import PlatformClient, PlatformClientError
        from radioinspector.windows.monitoring import _download
        from tests.test_device_workers import simulated_session_payload
        server = create_server(host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            session = server.session_service.create_session(simulated_session_payload())
            project = server.monitoring.create(dict(name='Audio', site='Fixture', session_id=session.id))
            raw = Client().monitoring_event_audio(project['id'], 'fixture')
            artifact = server.session_service.save_instrument_artifact(session.id, session.streams[1].id,
                dict(kind='audio', mime_type='audio/wav', filename='fixture.wav',
                     data_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest(),
                     metadata={'occurred_at':'2026-11-10T00:00:00Z', 'synthetic':True}))
            server.monitoring.store.put('event', dict(id='receiver-event', project_id=project['id'],
                created_at='2026-11-10T00:00:00Z', analysis={'audio':dict(id=artifact['id'], session_id=session.id,
                    stream_id=session.streams[1].id)}))
            client = PlatformClient(f'http://127.0.0.1:{server.server_port}')
            self.assertEqual(_download(client, dict(project_id=project['id'], event_id='receiver-event')), raw)
            with self.assertRaises(PlatformClientError) as error:
                _download(client, dict(project_id='foreign-project', event_id='receiver-event'))
            self.assertEqual(error.exception.status, 404)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

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


def settings():
    return dict(autostart=False, archive=dict(minimum_free_bytes=268435456, minimum_retention_seconds=3600),
                webhook=dict(enabled=False,url='',secret_configured=False), detector=dict(margin_db=10))


def storage():
    return dict(capacity=None,filesystem_free_bytes=1024**3,minimum_free_bytes=268435456,available_bytes=805306368,
                bytes_per_second=None,estimated_retention_seconds=None,required_retention_seconds=3600,ready=False,automatic_deletion=False)


def alert(identifier, **changes):
    return dict(id=identifier,event_id='e1',severity='warning',created_at='2026-11-10T12:00:00Z',kind='new_signal',
                message='Обнаружено излучение',acknowledged_at=None,
                delivery=dict(state='disabled',attempts=0,next_attempt_at=None,last_error=None),**changes) if not changes else dict(alert(identifier),**changes)


class MonitoringProjectActionScopeTests(unittest.TestCase):
    def test_background_read_never_blocks_or_completes_operator_action(self):
        from concurrent.futures import Future
        from radioinspector.windows.app import WindowsApp
        from tests.test_windows_tk_lifecycle import TclRoot
        root=TclRoot()
        try:
            with patch.object(WindowsApp,'_build'),patch('radioinspector.windows.app.ThreadPoolExecutor'):
                app=WindowsApp(root,Mock(),Mock())
            app.refresh=Mock();app._render_controls=Mock();app.action_var=Mock();app.client=Client()
            read=Future();app._executor.submit.return_value=read
            app._submit_project_read('poll',lambda:{},lambda data:None)
            self.assertFalse(app._action_running)
            action=Future();app._executor.submit.return_value=action
            app._submit_project_action('disarm',lambda:{},lambda data:None)
            self.assertTrue(app._action_running)
            read.set_result({});app._drain_messages();self.assertTrue(app._action_running)
            action.set_result({});app._drain_messages();self.assertFalse(app._action_running)
            app.close()
        finally:root.dispose()

    def test_project_http_callback_survives_session_switch_but_not_endpoint_switch(self):
        from concurrent.futures import Future
        from radioinspector.windows.app import WindowsApp
        from tests.test_windows_tk_lifecycle import TclRoot
        root = TclRoot()
        try:
            with patch.object(WindowsApp, '_build'), patch('radioinspector.windows.app.ThreadPoolExecutor'):
                app = WindowsApp(root, Mock(), Mock())
            app.refresh = Mock(); app._render_controls = Mock(); app.action_var = Mock()
            app.client = Client()
            future = Future(); app._executor.submit.return_value = future
            received = []
            app._submit_project_action('project', lambda: {}, received.append)
            app._revision += 1; app.session_id = 'other-session'
            future.set_result({'retained':True}); app._drain_messages()
            self.assertEqual(received, [{'retained':True}])
            future = Future(); app._executor.submit.return_value = future
            app._submit_project_action('project', lambda: {}, received.append)
            app.client = Client(); app._revision += 1
            future.set_result({'stale':True}); app._drain_messages()
            self.assertEqual(received, [{'retained':True}])
            app.close()
            self.assertEqual(root.pending(), ())
        finally:
            root.dispose()


def detector_parameters():
    return dict(margin_db=6.0,impulse_db=12.0,hysteresis_db=3.0,confirmations=3,window_sweeps=5,dwell_seconds=10.0)


def prepared_project(confirmed=True):
    return dict(id='p1',state='ready',active_session_id='original-source',
        profile=dict(analyzer=dict(settings={'adapter':'tinysa-serial'}),receiver=dict(settings={'adapter':'ardv1-serial'})),
        preparation=dict(antenna_notes='Приёмная антенна; кабель проверен',receive_path_confirmed=confirmed,location_confirmed=confirmed))


def collection_progress(sweeps,seconds,ready):
    return dict(project_id='p1',ready=ready,checks=[],hardware_validation='not_run',
                baseline_collection=dict(sweeps=sweeps,elapsed_seconds=seconds,minimum_sweeps=20,minimum_seconds=60,ready=ready))
