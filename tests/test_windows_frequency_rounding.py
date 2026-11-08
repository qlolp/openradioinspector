"""Synthetic transport, corruption and lifecycle coverage; not Windows visual QA."""
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import queue
import tempfile
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from radioinspector.contracts import api_payload
from radioinspector.windows.client import MAX_RESPONSE_BYTES, PlatformClient, PlatformClientError
from radioinspector.windows.frequency_rounding import (
    ExportGuard, FrequencyRoundingPanel, MAX_HZ, candidate_source, check_draft,
    profile_template, read_profile_file, result_text, save_profile_file,
    validate_profile, validate_result, validate_returned_draft,
)


def profile():
    return json.loads(profile_template())


def source(frequency=103025123, identifier='candidate', unit='dBm'):
    return dict(id=identifier, session_id='session', source_stream_id='receiver' if unit == 's_meter_count' else 'spectrum',
                center_frequency_hz=frequency, level_unit=unit, state='new', score=5, peak_level=37,
                interval={'started_at': '2026-11-08T12:00:00.000001Z', 'ended_at': '2026-11-08T12:00:01.000002Z'})


def response(src=None, rules=None, status='rounded', index=0, nearest=103000000, offset=-25123, rounded=103000000):
    return dict(session_id='session', candidate=candidate_source(src or source(), 'session'),
                profile=rules or profile(), frequency_unit='Hz', method='nearest_grid', tie_break='lower',
                result=dict(status=status, rule_index=index, nearest_frequency_hz=nearest,
                            offset_hz=offset, rounded_frequency_hz=rounded))


class Var:
    def __init__(self, value=''):
        self.value = value
        self.callback = None

    def get(self): return self.value

    def set(self, value):
        self.value = value
        if self.callback: self.callback()


def panel():
    p = FrequencyRoundingPanel.__new__(FrequencyRoundingPanel)
    p.app = SimpleNamespace(client=Mock(), session_id='session', selected_stream_id=lambda: 'spectrum', root=Mock(),
        server_var=Mock(), snapshot=SimpleNamespace(session={'id': 'session'}, candidates=[source()]))
    p.scope = (id(p.app.client), 'session')
    p.generation = 0
    p.future = p.retired = p.io_future = p.io_guard = None
    p.data = p.source = p.chosen_source = None
    p.closed = p.snapshot_unavailable = False
    p.server_edit_client = None
    p.server_trace = 'trace'
    p.executor = Mock(); p.executor.submit.side_effect = lambda *args: Future()
    p.io_executor = Mock(); p.io_executor.submit.side_effect = lambda *args: Future()
    for key in ('button', 'save_button', 'details', 'editor', 'candidates'):
        setattr(p, key, Mock())
    p.info, p.retained_info, p.candidate = Var(), Var(), Var()
    p.candidate.callback = p.choose_candidate
    p.editor.get.return_value = profile_template()
    p.candidate.set('candidate')
    return p


def complete(p, data=None):
    p.analyze()
    p.future.set_result(response() if data is None else data)
    p.finish(p.generation)


class ValidationTests(unittest.TestCase):
    def check(self, data, src=None, raw=None):
        return validate_result(data, 'session', src or source(), raw or profile_template())

    def test_template_exact_portable_example(self):
        example = Path(__file__).resolve().parents[1] / 'docs/examples/frequency-rounding-profile.json'
        self.assertEqual(profile(), json.loads(example.read_text(encoding='utf-8')))
        self.check(response())

    def test_all_statuses_and_original_units(self):
        cases = [(103025123, 'dBm', 'rounded', 0, 103000000, -25123, 103000000),
                 (103000000, 'dBm', 'unchanged', 0, 103000000, 0, 103000000),
                 (103050000, 'dBm', 'outside_tolerance', 0, 103000000, -50000, None),
                 (120000000, 'dBm', 'outside_ranges', None, None, None, None),
                 (145506250, 's_meter_count', 'rounded', 1, 145500000, -6250, 145500000),
                 (103075000, 'dBm', 'rounded', 0, 103100000, 25000, 103100000)]
        for hz, unit, status, index, nearest, offset, rounded in cases:
            with self.subTest(status=status, hz=hz):
                src = source(hz, unit=unit)
                data = response(src, status=status, index=index, nearest=nearest, offset=offset, rounded=rounded)
                self.check(data, src)
                text = result_text(data)
                for value in (str(hz), status, unit, '.000001Z', '.000002Z'):
                    self.assertIn(value, text)
                if offset is not None: self.assertIn(f'{offset:+d} Hz', text)
                self.assertNotIn('peak_level', data['candidate'])
                self.assertNotIn('state', data['candidate'])

    def test_exact_integer_grid_edges_negative_indices_zero_and_maxsafe(self):
        cases = [
            # start/end inclusive; origin may lie beyond the range.
            (10, 0, 20, 10, 20, 10, 10),
            (15, 10, 20, 10, 30, 5, 10),
            (20, 11, 21, 10, 30, 0, 20),
            # Nearest extended grid point is excluded, so use nearest in-range point.
            (11, 11, 21, 10, 0, 9, 20),
            (1, 0, 9, 10, 0, 1, 0),
            (MAX_HZ, MAX_HZ-2, MAX_HZ, 3, MAX_HZ, 0, MAX_HZ),
            (MAX_HZ-1, 0, MAX_HZ, MAX_HZ, MAX_HZ, 1, MAX_HZ),
        ]
        for original, start, end, step, origin, tolerance, nearest in cases:
            with self.subTest(original=original, start=start):
                artifact = profile(); artifact['rules'] = [dict(start_frequency_hz=start, end_frequency_hz=end,
                    step_hz=step, origin_frequency_hz=origin, max_offset_hz=tolerance)]
                src = source(original); offset = nearest-original
                data = response(src, artifact, status='unchanged' if not offset else 'rounded',
                                nearest=nearest, offset=offset, rounded=nearest)
                self.check(data, src, json.dumps(artifact))
                self.assertIn(f'Производная частота: {nearest} Hz', result_text(data))

    def test_normalization_duplicates_and_strict_profile(self):
        draft = profile(); draft['name'] = '  e\u0301  '
        canonical = profile(); canonical['name'] = 'é'
        validate_returned_draft(json.dumps(draft), canonical)
        for raw in ('{}', '[]', 'null', '{bad', '{"name":"a","name":"b"}', '{"name":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_returned_draft(raw, profile())
        for key, value in [('format', 'wrong'), ('format_version', True), ('format_version', 1.0),
                ('name', 'a\n'), ('name', ' a '), ('name', 'e\u0301'), ('name', 'x'*81),
                ('name', '\ud800'), ('name', '\u200b'), ('frequency_unit', 'MHz'),
                ('method', 'floor'), ('tie_break', 'upper'), ('rules', []), ('extra', 1)]:
            artifact = profile(); artifact[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError): validate_profile(artifact)
        for key in profile()['rules'][0]:
            for value in (-1, MAX_HZ+1, True, 1.0, '1', None):
                artifact = profile(); artifact['rules'][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError): validate_profile(artifact)
        for change in (lambda a:a['rules'][0].update(step_hz=0),
                       lambda a:a['rules'][0].update(extra=0),
                       lambda a:a['rules'][0].update(start_frequency_hz=110000001),
                       lambda a:a['rules'][1].update(start_frequency_hz=110000000),
                       lambda a:a['rules'].reverse(),
                       lambda a:a['rules'][0].update(start_frequency_hz=1, end_frequency_hz=9, step_hz=10)):
            artifact = profile(); change(artifact)
            with self.assertRaises(ValueError): validate_profile(artifact)
        for count in (128, 129):
            artifact = profile(); artifact['rules'] = [dict(start_frequency_hz=i, end_frequency_hz=i,
                step_hz=1, origin_frequency_hz=0, max_offset_hz=0) for i in range(count)]
            if count == 128: validate_profile(artifact)
            else:
                with self.assertRaises(ValueError): validate_profile(artifact)
        bad = profile(); bad['name'] = 'different'
        with self.assertRaises(ValueError): validate_returned_draft(profile_template(), bad)

    def test_all_response_fields_and_arithmetic_are_verified(self):
        changes = [lambda r:r.update(session_id='other'), lambda r:r.update(frequency_unit='MHz'),
                   lambda r:r.update(method='floor'), lambda r:r.update(tie_break='upper'),
                   lambda r:r.update(extra=True), lambda r:r['candidate'].update(state='new'),
                   lambda r:r['profile'].update(name='wrong')]
        changes += [lambda r,k=k,v=v:r['result'].update({k:v}) for k,v in [
            ('status','unchanged'), ('rule_index',True), ('rule_index',1), ('nearest_frequency_hz',103100000),
            ('offset_hz',-25122), ('rounded_frequency_hz',None), ('offset_hz',-25123.0), ('extra',0)]]
        changes += [lambda r,k=k,v=v:r['candidate'].update({k:v}) for k,v in [
            ('id','other'), ('source_stream_id','other'), ('center_frequency_hz',103025124),
            ('center_frequency_hz',103025123.0), ('level_unit','s_meter_count')]]
        for change in changes:
            data = response(); change(data)
            with self.subTest(change=change), self.assertRaises(ValueError): self.check(data)
        for stamp in ('2026-11-08T12:00:00.000002Z', '2026-11-08T12:00:00.0000011Z',
                      '2026-11-08T13:00:00.000001+01:00', '2026-11-08 12:00:00Z'):
            data = response(); data['candidate']['interval']['started_at'] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(ValueError): self.check(data)
        data = response(); data['candidate']['interval'] = {k:v.replace('Z', '+00:00') for k,v in data['candidate']['interval'].items()}
        self.check(data)

    def test_immutable_provenance_and_unbounded_scope(self):
        src = source(); src.update(state='dismissed', score=99, peak_level=-10, observations=['new'])
        self.check(response(), src)
        session = 'session/'+'𐀀'*200; stream = 'receiver/'+'я'*200
        src = source(identifier='𐀀'*128); src.update(session_id=session, source_stream_id=stream)
        data = response(); data.update(session_id=session, candidate=candidate_source(src, session))
        validate_result(data, session, src, profile_template())
        for key, value in [('id','𐀀'*129), ('id','\ud800'), ('id',' '), ('source_stream_id',''),
                ('center_frequency_hz',True), ('center_frequency_hz',0), ('center_frequency_hz',MAX_HZ+1),
                ('session_id','foreign'), ('level_unit','')]:
            src = source(); src[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError): candidate_source(src, 'session')
        src = source(identifier='e\u0301'); data = response(src)
        data['candidate']['id'] = 'é'
        with self.assertRaises(ValueError): self.check(data, src)


class FileTests(unittest.TestCase):
    def test_import_raw_utf8_bound_and_canonical_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rounding.json'
            for raw in ('{bad', '{"name":"x","name":"x"}', '', 'é'*32768):
                path.write_text(raw, encoding='utf-8'); self.assertEqual(read_profile_file(path), raw)
            for raw, error in [(b'x'*65537, ValueError), (b'\xff', UnicodeDecodeError)]:
                path.write_bytes(raw)
                with self.assertRaises(error): read_profile_file(path)
            save_profile_file(path, profile(), True)
            self.assertEqual(json.loads(path.read_text()), profile())
            before = path.read_bytes()
            with self.assertRaises(FileExistsError): save_profile_file(path, profile())
            self.assertEqual(path.read_bytes(), before)
            for operation in ('replace', 'fsync'):
                with patch('radioinspector.windows.frequency_rounding.os.'+operation, side_effect=OSError('synthetic')):
                    with self.assertRaises(OSError): save_profile_file(path, profile(), True)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(Path(directory).iterdir()), [path])
        for raw in ('', ' ', 'x'*65537, '\ud800'):
            with self.assertRaises((ValueError, UnicodeError)): check_draft(raw)

    def test_atomic_guard_prevents_delayed_save_and_leaves_no_temporary(self):
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=1) as worker:
            path = Path(directory)/'rounding.json'; path.write_bytes(b'original')
            entered, release = Event(), Event()
            def delay(_fd):
                entered.set(); self.assertTrue(release.wait(3))
            guard = ExportGuard()
            with patch('radioinspector.windows.frequency_rounding.os.fsync', side_effect=delay):
                future = worker.submit(save_profile_file, path, profile(), True, guard)
                self.assertTrue(entered.wait(3)); guard.cancel(); release.set()
                self.assertFalse(future.result(timeout=3))
            self.assertEqual(path.read_bytes(), b'original')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_atomic_create_does_not_replace_late_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rounding.json'
            def late_destination(_fd): path.write_bytes(b'late')
            with patch('radioinspector.windows.frequency_rounding.os.fsync', side_effect=late_destination):
                with self.assertRaises(FileExistsError): save_profile_file(path, profile())
            self.assertEqual(path.read_bytes(), b'late')
            self.assertEqual(list(Path(directory).iterdir()), [path])


class LifecycleTests(unittest.TestCase):
    def test_explicit_session_candidates_independent_of_spectrum_stream(self):
        p = panel(); receiver = source(145506250, 'receiver-candidate', 's_meter_count')
        p.app.snapshot.candidates += [receiver, dict(source(), id='foreign', session_id='other')]
        p.update(); p.executor.submit.assert_not_called()
        self.assertEqual([s['id'] for s in p.sources()], ['candidate', 'receiver-candidate'])
        p.candidate.set('receiver-candidate'); p.analyze(); p.analyze()
        p.executor.submit.assert_called_once_with(p.app.client.candidate_frequency_rounding,
                                                  'session', 'receiver-candidate', profile_template())
        result = response(receiver, index=1, nearest=145500000, offset=-6250, rounded=145500000)
        p.future.set_result(result); p.finish(p.generation)
        self.assertEqual(p.data, result)
        self.assertEqual(p.source['level_unit'], 's_meter_count')

    def test_selection_provenance_visible_before_request_and_spectrum_switch_preserves_it(self):
        p = panel()
        for token in ('candidate', 'spectrum', '103025123 Hz', 'dBm', '.000001Z', '.000002Z'):
            self.assertIn(token, p.retained_info.get())
        p.executor.submit.assert_not_called()
        p.analyze(); generation = p.generation; chosen = deepcopy(p.chosen_source)
        p.app.selected_stream_id = lambda: 'receiver'
        p.update()
        self.assertEqual(p.generation, generation)
        self.assertEqual(p.chosen_source, chosen)
        p.future.set_result(response()); p.finish(generation)
        self.assertEqual(p.data, response())
        p.app.selected_stream_id = lambda: None
        p.update()
        self.assertEqual(p.data, response())
        self.assertEqual(p.chosen_source, chosen)
        p.save_button.configure.assert_called_with(state='normal')

    def test_full_chosen_source_survives_mutable_polling_page_removal_and_reload(self):
        p = panel(); chosen = deepcopy(p.chosen_source); p.analyze()
        p.app.snapshot.candidates[0].update(state='dismissed', score=55, center_frequency_hz=999)
        p.app.snapshot.candidates = []; p.update()
        self.assertEqual(p.chosen_source, chosen)
        self.assertIn('candidate', p.candidates.configure.call_args.kwargs['values'])
        self.assertIn('вне текущей страницы', p.retained_info.get())
        p.future.set_result(response()); p.finish(p.generation)
        self.assertEqual(p.data, response())
        p.clear(); self.assertEqual(p.chosen_source, chosen)
        complete(p); self.assertEqual(p.data, response())
        p.editor.get.return_value = profile_template()+' '
        p.edited(); self.assertIsNone(p.data); self.assertEqual(p.chosen_source, chosen)
        complete(p); self.assertEqual(p.executor.submit.call_count, 3)

    def test_each_invalidation_discards_late_results_and_cancels_exports(self):
        for action in ('clear', 'edit', 'candidate', 'session', 'server', 'server_edit', 'disconnect', 'timeout', 'close'):
            with self.subTest(action=action):
                p = panel(); p.analyze(); old = p.future; old.set_running_or_notify_cancel(); generation = p.generation
                p.io_guard = guard = ExportGuard()
                if action in ('clear', 'close'): getattr(p, action)()
                elif action == 'edit': p.edited()
                elif action == 'candidate': p.candidate.set('other')
                elif action == 'session': p.app.session_id = 'other'
                elif action == 'server': p.app.client = Mock()
                elif action == 'server_edit': p.disconnect('server', '', 'write')
                elif action == 'disconnect': p.disconnect()
                elif action == 'timeout': p.deadline = monotonic()-1
                p.finish(generation); p.analyze()
                self.assertEqual(p.executor.submit.call_count, 1)
                self.assertTrue(guard.cancelled)
                old.set_result(response()); p.finish(generation)
                self.assertIsNone(p.data)
                p.save_button.configure.assert_called_with(state='disabled')

    def test_retired_singleflight_then_explicit_retry(self):
        p = panel(); p.analyze(); old = p.future; old.set_running_or_notify_cancel()
        p.clear(); p.analyze(); self.assertEqual(p.executor.submit.call_count, 1)
        old.set_result(response()); complete(p)
        self.assertEqual(p.executor.submit.call_count, 2)
        self.assertEqual(p.data, response())

    def test_retained_candidate_reselection_preserves_full_source(self):
        p = panel(); complete(p)
        chosen = deepcopy(p.chosen_source)
        p.app.snapshot.candidates = []; p.update()
        p.candidate.set('candidate')
        self.assertEqual(p.chosen_source, chosen)
        self.assertIsNone(p.data)
        self.assertIn('вне текущей страницы', p.retained_info.get())
        complete(p); self.assertEqual(p.data, response())
        p.candidate.set('other'); p.candidate.set('candidate')
        self.assertIsNone(p.chosen_source)

    def test_errors_and_corruption_disable_export_but_allow_retry(self):
        for status in (400, 404, 409, 413, 500):
            p = panel(); p.analyze()
            p.future.set_exception(PlatformClientError('synthetic', status=status))
            p.finish(p.generation); self.assertIsNone(p.data)
            p.save_button.configure.assert_called_with(state='disabled')
            complete(p); self.assertIsNotNone(p.data)
        p = panel(); bad = response(); bad['result']['offset_hz'] += 1
        complete(p, bad); self.assertIsNone(p.data)
        p = panel(); p.analyze(); p.editor.get.return_value = '{}'
        p.future.set_result(response()); p.finish(p.generation); self.assertIsNone(p.data)

    def test_actual_refresh_error_gates_sources_until_successful_refresh(self):
        from radioinspector.windows.app import WindowsApp
        for running in (False, True):
            for rolled in (False, True):
                with self.subTest(running=running, rolled=rolled):
                    p = panel(); p.analyze(); old = p.future; generation = p.generation
                    if running: old.set_running_or_notify_cancel()
                    else: old.set_result(response()); p.finish(generation)
                    if rolled: p.app.snapshot.candidates = []
                    app = WindowsApp.__new__(WindowsApp)
                    app._closed = False; app._revision = 7; app._messages = queue.Queue()
                    app.root = Mock(); app.connection_var = Mock(); app._show_archive_capacity = Mock()
                    app._render_controls = Mock(); app.frequency_rounding_panel = p; app._refresh_running = True
                    failed = Future(); failed.set_exception(PlatformClientError('offline'))
                    app._messages.put(('snapshot', failed, 7, '')); app._drain_messages()
                    self.assertIsNone(p.data); self.assertIsNone(p.chosen_source)
                    self.assertEqual(p.candidate.get(), '')
                    if running: old.set_result(response()); p.finish(generation)
                    p.app.snapshot.candidates = [source()]
                    p.candidate.set('candidate'); p.update(); p.analyze()
                    self.assertIsNone(p.chosen_source); self.assertEqual(p.sources(), [])
                    self.assertEqual(p.executor.submit.call_count, 1)
                    fresh = Future(); fresh.set_result(p.app.snapshot)
                    app._apply_snapshot = Mock()
                    app._messages.put(('snapshot', fresh, 7, '')); app._drain_messages()
                    self.assertIsNone(p.chosen_source)  # No stale automatic repin.
                    p.candidate.set('candidate'); complete(p)
                    self.assertIsNotNone(p.data)

    def test_server_edit_old_client_refresh_cannot_repin(self):
        p = panel(); p.disconnect('server', '', 'write'); p.snapshot_refreshed()
        p.candidate.set('candidate'); p.analyze()
        self.assertEqual(p.sources(), []); p.executor.submit.assert_not_called()
        p.app.client = Mock(); p.snapshot_refreshed()
        p.candidate.set('candidate'); complete(p); self.assertIsNotNone(p.data)

    def test_import_dialog_and_inflight_late_result_guards(self):
        p = panel(); complete(p)
        with patch('tkinter.filedialog.askopenfilename', return_value=''): p.load()
        self.assertEqual(p.data, response())
        p.io_executor.submit.assert_not_called()
        p = panel()
        def changed_dialog(**kwargs): p.candidate.set('other'); return 'profile.json'
        with patch('tkinter.filedialog.askopenfilename', side_effect=changed_dialog): p.load()
        p.io_executor.submit.assert_not_called()
        for action in ('clear', 'edit', 'candidate', 'session', 'disconnect', 'close'):
            p = panel()
            with patch('tkinter.filedialog.askopenfilename', return_value='profile.json'): p.load()
            old, generation = p.io_future, p.generation
            old.set_running_or_notify_cancel()
            if action in ('clear', 'close'): getattr(p, action)()
            elif action == 'edit': p.edited()
            elif action == 'candidate': p.candidate.set('other')
            elif action == 'session': p.app.session_id = 'other'
            else: p.disconnect()
            old.set_result('late imported raw'); p.finish_io(generation, 'load')
            p.editor.insert.assert_not_called()
        p = panel()
        with patch('tkinter.filedialog.askopenfilename', return_value='profile.json'): p.load()
        p.io_future.set_result('{bad raw'); p.finish_io(p.generation, 'load')
        p.editor.insert.assert_called_once_with('1.0', '{bad raw')
        p.executor.submit.assert_not_called()

    def test_export_only_matching_canonical_response_with_modal_guards(self):
        p = panel()
        with patch('tkinter.filedialog.asksaveasfilename') as dialog:
            p.save(); dialog.assert_not_called()
        complete(p)
        def changed_dialog(**kwargs): p.clear(); return 'result.json'
        with patch('tkinter.filedialog.asksaveasfilename', side_effect=changed_dialog): p.save()
        p.io_executor.submit.assert_not_called()
        complete(p)
        with patch('tkinter.filedialog.asksaveasfilename', return_value='result.json'), patch('os.path.lexists', return_value=False): p.save()
        args = p.io_executor.submit.call_args.args
        self.assertEqual(args[:4], (save_profile_file, 'result.json', profile(), False))
        self.assertIsInstance(args[4], ExportGuard)
        p.clear(); self.assertTrue(args[4].cancelled)
        p.io_future.set_result(False); p.finish_io(p.generation-1, 'save')
        self.assertNotIn('Сохранён', p.info.get())
        p = panel(); complete(p)
        def reject_after_edit(*args, **kwargs): p.edited(); return True
        with patch('tkinter.filedialog.asksaveasfilename', return_value='result.json'), patch('os.path.lexists', return_value=True), patch('tkinter.messagebox.askyesno', side_effect=reject_after_edit): p.save()
        p.io_executor.submit.assert_not_called()


class HTTPTests(unittest.TestCase):
    def client(self, raw, status=200):
        stream = io.BytesIO(raw); stream.status = status
        opener = Mock(); opener.open.return_value = stream
        return PlatformClient('http://localhost:56040', opener=opener), opener

    def test_exact_post_strict_json_statuses_and_timeout(self):
        client, opener = self.client(json.dumps(api_payload(response())).encode())
        client.timeout = 120
        self.assertEqual(client.candidate_frequency_rounding('session/я', '𐀀'*128, profile_template()), response())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertTrue(request.full_url.endswith('/sessions/session%2F%D1%8F/candidate-frequency-rounding'))
        self.assertEqual(json.loads(request.data), {'candidate_id':'𐀀'*128, 'profile_json':profile_template()})
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 8.0)
        for status in (201, 202, 204, 400, 404, 409, 413, 500):
            client, _ = self.client(json.dumps(api_payload(response())).encode(), status)
            with self.subTest(status=status), self.assertRaises(PlatformClientError):
                client.candidate_frequency_rounding('session', 'candidate', profile_template())
        for raw in (b'{"data":{},"data":{}}', b'{"data":NaN}', b'\xff'):
            client, _ = self.client(raw)
            with self.assertRaises(PlatformClientError) as error:
                client.candidate_frequency_rounding('session', 'candidate', profile_template())
            self.assertEqual(error.exception.code, 'invalid_json')
        data = api_payload(response()); data['contract_version'] = True
        client, _ = self.client(json.dumps(data).encode())
        with self.assertRaises(PlatformClientError): client.candidate_frequency_rounding('session','candidate',profile_template())
        for extra in ('extra', 'error'):
            data = api_payload(response()); data[extra] = {}
            client, _ = self.client(json.dumps(data).encode())
            with self.assertRaises(PlatformClientError) as error:
                client.candidate_frequency_rounding('session','candidate',profile_template())
            self.assertEqual(error.exception.code, 'invalid_envelope')

    def test_all_statuses_bounded_before_decoding_including_http_errors(self):
        for status in (200, 202, 400, 500):
            client, _ = self.client(b'\xff'*(MAX_RESPONSE_BYTES+1), status)
            with self.assertRaises(PlatformClientError) as error:
                client.candidate_frequency_rounding('session','candidate',profile_template())
            self.assertEqual(error.exception.code, 'response_too_large')
        for status in (400, 404, 409, 413, 500):
            client, opener = self.client(b'')
            opener.open.side_effect = urllib.error.HTTPError('http://localhost', status, 'test', {}, io.BytesIO(b'\xff'*(MAX_RESPONSE_BYTES+1)))
            with self.assertRaises(PlatformClientError) as error:
                client.candidate_frequency_rounding('session','candidate',profile_template())
            self.assertEqual(error.exception.code, 'response_too_large')
        client, opener = self.client(b'')
        opener.open.side_effect = urllib.error.HTTPError('http://localhost', 500, 'test', {}, io.BytesIO(b'{"error":[]}'))
        with self.assertRaises(PlatformClientError): client.candidate_frequency_rounding('session','candidate',profile_template())

    def test_request_invalid_ids_and_long_scope(self):
        for session, candidate, raw in [('','candidate','{}'), ('session','𐀀'*129,'{}'),
                ('session','\ud800','{}'), ('session','candidate','x'*65537)]:
            client, opener = self.client(b'')
            with self.assertRaises(ValueError): client.candidate_frequency_rounding(session,candidate,raw)
            opener.open.assert_not_called()
        session = 'session-'+'x'*200
        client, opener = self.client(json.dumps(api_payload(response())).encode())
        client.candidate_frequency_rounding(session,'candidate',profile_template())
        self.assertIn('/sessions/'+session+'/', opener.open.call_args.args[0].full_url)

    def test_real_synthetic_http_transport(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                body = json.dumps(api_payload(response())).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = PlatformClient(f'http://127.0.0.1:{server.server_port}')
            for _ in range(2): validate_result(client.candidate_frequency_rounding('session','candidate',profile_template()), 'session', source(), profile_template())
            self.assertEqual(seen, [('/api/v1/sessions/session/candidate-frequency-rounding',
                {'candidate_id':'candidate','profile_json':profile_template()})]*2)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    @unittest.skipUnless(os.getenv('ORI_ROUNDING_SMOKE_BASE_URL'), 'Optional synthetic core HTTP fixture')
    def test_live_synthetic_all_statuses_and_units(self):
        client = PlatformClient(os.environ['ORI_ROUNDING_SMOKE_BASE_URL'])
        candidates = client.list_candidates('rounding-one')
        statuses = {'one-rounded':'rounded', 'one-unchanged':'unchanged', 'one-tolerance':'outside_tolerance',
                    'one-outside':'outside_ranges', 'one-receiver':'rounded', 'one-upper':'rounded'}
        for candidate in candidates:
            data = client.candidate_frequency_rounding('rounding-one',candidate['id'],profile_template())
            validate_result(data,'rounding-one',candidate,profile_template())
            self.assertEqual(data['result']['status'], statuses[candidate['id']])


@unittest.skipUnless(os.getenv('ORI_ROUNDING_SMOKE_BASE_URL') and os.getenv('ORI_ROUNDING_TK_SMOKE'),
                     'Opt-in own hidden Tk panel, not visual or Windows OS QA')
class ActualTkHTTPTests(unittest.TestCase):
    def test_hidden_panel_real_http_retention_selection_edit_and_disconnect(self):
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk(); root.withdraw()
        client = PlatformClient(os.environ['ORI_ROUNDING_SMOKE_BASE_URL'])
        app = SimpleNamespace(tk=tk, ttk=ttk, root=root, client=client, session_id='rounding-one',
            selected_stream_id=lambda:'spectrum-one', server_var=tk.StringVar(value=client.base_url),
            snapshot=SimpleNamespace(session={'id':'rounding-one'}, candidates=client.list_candidates('rounding-one')))
        notebook = ttk.Notebook(root); notebook.pack()
        p = FrequencyRoundingPanel(app, notebook)
        def finish():
            until = monotonic()+10
            while p.future is not None and monotonic() < until:
                root.update(); sleep(.02)
            self.assertIsNotNone(p.data, p.info.get())
        try:
            p.update(); p.candidate.set('one-rounded'); p.analyze(); p.analyze(); finish()
            self.assertEqual(p.data['result']['offset_hz'], -25123)
            self.assertIn('103025123 Hz', p.details.get('1.0', 'end'))
            app.snapshot.candidates = []; p.update()
            self.assertIn('вне текущей страницы', p.retained_info.get())
            p.clear(); p.analyze(); finish()
            app.snapshot.candidates = client.list_candidates('rounding-one')
            p.candidate.set('one-receiver'); p.analyze(); finish()
            self.assertEqual(p.data['candidate']['level_unit'], 's_meter_count')
            p.editor.insert('end', ' '); root.update(); self.assertIsNone(p.data)
            self.assertEqual(str(p.save_button['state']), 'disabled')
            p.analyze(); finish()
            app.server_var.set('http://localhost:56040'); p.update(); p.snapshot_refreshed(); p.analyze()
            self.assertIsNone(p.data); self.assertIsNone(p.chosen_source)
            self.assertEqual(p.candidate.get(), ''); self.assertEqual(str(p.button['state']), 'disabled')
        finally:
            p.close(); root.destroy()
