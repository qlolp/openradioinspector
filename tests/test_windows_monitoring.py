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

    def monitoring(self, path='', payload=None):
        self.calls.append((path, payload))
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
