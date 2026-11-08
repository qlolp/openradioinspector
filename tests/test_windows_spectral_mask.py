import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from concurrent.futures import Future
from copy import deepcopy
from time import monotonic
from tests.test_windows_sweep_threshold import panel as threshold_panel, response as threshold_response
from radioinspector.windows.spectral_mask import (SpectralMaskPanel, check_draft, mask_template,
    read_mask_file, save_mask_file, validate_artifact, validate_mask_result, validate_returned_draft, mask_text)
from radioinspector.windows.client import PlatformClient, PlatformClientError


def artifact():
    return json.loads(mask_template({'frequencies_hz': [100, 300]}))


def response():
    r = threshold_response()
    r.pop('parameters'); r.pop('baseline_levels')
    r['mask'] = artifact(); r['comparison'] = 'strictly_greater'
    r['threshold_levels'] = [-80]*3; r['excess_levels'] = [30, 50, 30]
    r['exceedance_points'] = 3
    r['regions'][0].update(start_index=0, end_index=2, start_frequency_hz=100, end_frequency_hz=300, peak_excess_db=50, points=3)
    return r


def panel():
    base = threshold_panel()
    p = SpectralMaskPanel.__new__(SpectralMaskPanel)
    p.__dict__.update(base.__dict__)
    p.editor = Mock(); p.editor.get.return_value = json.dumps(artifact())
    p.save_button = Mock(); p.io_executor = Mock(); p.io_future = None
    return p


class MaskValidationTests(unittest.TestCase):
    def test_valid_response_and_canonical_identity(self):
        validate_mask_result(response(), ('session', 's', 'a'))
        raw = artifact(); raw['name'] = '  e\u0301  '
        canonical = artifact(); canonical['name'] = 'é'
        validate_returned_draft(json.dumps(raw), canonical)
        for raw in ('{"name":"x","name":"y"}', '{}'):
            with self.assertRaises(ValueError): validate_returned_draft(raw, artifact())
        with self.assertRaises(ValueError): validate_returned_draft(json.dumps(canonical), artifact())

    def test_artifact_strict(self):
        for key, value in [('format_version', True), ('extra', 1), ('name','\nname'), ('name',' a '), ('name','e\u0301'), ('vertices',[]), ('interpolation','linear')]:
            mask = artifact(); mask[key] = value
            with self.assertRaises(ValueError): validate_artifact(mask)
        for key, value in [('frequency_hz',True), ('frequency_hz',-1), ('frequency_hz',2**53), ('limit_dbm',float('nan')), ('limit_dbm',10**1000), ('extra',0)]:
            mask = artifact(); mask['vertices'][0][key] = value
            with self.assertRaises(ValueError): validate_artifact(mask)
        with self.assertRaises(ValueError): mask_template({'frequencies_hz':[1,1]})

    def test_response_rejections(self):
        for key, value in [('session_id','wrong'), ('stream_id','wrong'), ('level_unit','S'), ('comparison','greater_equal'), ('points',True), ('frequencies_hz',[100,200,2**53]), ('threshold_levels',[-80]*2), ('excess_levels',[0,0,0]), ('regions',[]), ('region_count',True)]:
            r=response(); r[key]=value
            with self.subTest(key=key), self.assertRaises(ValueError): validate_mask_result(r,('session','s','a'))
        for key in response()['regions'][0]:
            r=response();r['regions'][0][key] += 1
            with self.assertRaises(ValueError): validate_mask_result(r,('session','s','a'))
        r=response(); source=dict(r['sweep'], frequencies_hz=r['frequencies_hz'])
        validate_mask_result(r,('session','s','a'), source)
        for key,value in [('id','wrong'),('levels',[1,2,3]),('settings',{}),('interval',{})]:
            bad=deepcopy(source); bad[key]=value
            with self.assertRaises(ValueError): validate_mask_result(r,('session','s','a'),bad)

    def test_settings_provenance_is_type_aware_and_includes_sequence(self):
        r=response()
        r['sweep']['settings'].update(nested={'flags': [True, {'enabled': False}]}, sequence={'number': 7, 'nullable': None})
        source=dict(deepcopy(r['sweep']), frequencies_hz=r['frequencies_hz'])
        validate_mask_result(r, ('session','s','a'), source)
        for change in ('bool', 'nested_bool', 'sequence', 'missing_sequence'):
            changed=deepcopy(r)
            settings=changed['sweep']['settings']
            if change=='bool': settings['nested']['flags'][0]=1
            if change=='nested_bool': settings['nested']['flags'][1]['enabled']=0
            if change=='sequence': settings['sequence']['number']=8
            if change=='missing_sequence': settings.pop('sequence')
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_mask_result(changed, ('session','s','a'), source)
        changed=deepcopy(r); changed['sweep']['settings']['rbw_hz']=1000.0
        validate_mask_result(changed, ('session','s','a'), source)

    def test_text_region_limit(self):
        r=response(); r['regions'] *= 201; r['region_count']=201
        text=mask_text(r); self.assertEqual(text.count('Индексы'),200); self.assertIn('200 из 201',text)


class MaskFileTests(unittest.TestCase):
    def test_raw_import_roundtrip_limits_and_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'mask.json'
            for raw in ('{bad', '{"name":"x","name":"y"}', '', 'é'*32768):
                p.write_text(raw, encoding='utf-8'); self.assertEqual(read_mask_file(p),raw)
            p.write_bytes(b'x'*65537)
            with self.assertRaises(ValueError): read_mask_file(p)
            p.write_bytes(b'\xff')
            with self.assertRaises(UnicodeDecodeError): read_mask_file(p)
            save_mask_file(p,artifact(),True)
            self.assertEqual(json.loads(read_mask_file(p)),artifact())
            before=p.read_bytes()
            with self.assertRaises(FileExistsError): save_mask_file(p,artifact())
            self.assertEqual(p.read_bytes(),before)
            with patch('radioinspector.windows.spectral_mask.os.replace',side_effect=OSError('synthetic error')):
                with self.assertRaises(OSError): save_mask_file(p,artifact(),True)
            self.assertEqual(p.read_bytes(),before)
            self.assertEqual(list(Path(directory).iterdir()),[p])
            with patch('radioinspector.windows.spectral_mask.os.fsync',side_effect=OSError('partial write')):
                with self.assertRaises(OSError): save_mask_file(p,artifact(),True)
            self.assertEqual(p.read_bytes(),before)
            self.assertEqual(list(Path(directory).iterdir()),[p])
            with self.assertRaises(OSError): read_mask_file(Path(directory)/'missing')

    def test_short_write_does_not_replace_existing(self):
        original = tempfile.NamedTemporaryFile
        def short_file(*args, **kwargs):
            file = original(*args, **kwargs)
            wrapped = Mock(wraps=file)
            wrapped.name = file.name
            wrapped.__enter__ = Mock(return_value=wrapped)
            wrapped.__exit__ = Mock(side_effect=lambda *ignored: file.close())
            wrapped.write.side_effect = lambda payload: file.write(payload[:-1])
            return wrapped
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'mask.json'; path.write_bytes(b'existing')
            with patch('radioinspector.windows.spectral_mask.tempfile.NamedTemporaryFile', side_effect=short_file):
                with self.assertRaises(OSError): save_mask_file(path, artifact(), True)
            self.assertEqual(path.read_bytes(), b'existing')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_dialog_cancel_and_declined_overwrite(self):
        p=panel();p.data=response()
        with patch('tkinter.filedialog.askopenfilename',return_value=''):
            p.load()
        with patch('tkinter.filedialog.asksaveasfilename',return_value=''):
            p.save()
        with patch('tkinter.filedialog.asksaveasfilename',return_value='synthetic.json'), patch('os.path.lexists',return_value=True), patch('tkinter.messagebox.askyesno',return_value=False):
            p.save()
        p.io_executor.submit.assert_not_called()

    def test_background_import_error_and_edit_race(self):
        p=panel(); p.io_future=Future(); p.io_future.set_exception(OSError('import failed'))
        p.finish_io(p.generation,'load'); self.assertIn('Ошибка файла', p.info.set.call_args.args[0])
        p=panel(); generation=p.generation; p.io_future=Future(); p.io_future.set_result('old')
        p.clear(); p.finish_io(generation,'load'); p.editor.insert.assert_not_called()


class MaskLifecycleTests(unittest.TestCase):
    def test_explicit_single_flight_frozen(self):
        p=panel();p.update();p.executor.submit.assert_not_called()
        raw=p.editor.get.return_value
        p.analyze();p.analyze();p.executor.submit.assert_called_once_with(p.app.client.sweep_mask_analysis,'session','a',raw)
        p.app.snapshot.sweeps=[];p.future.set_result(response());p.finish(p.generation)
        self.assertEqual(p.data['sweep']['id'],'a')
        self.assertEqual(p.canvas.create_line.call_count,2)
        for call in p.canvas.create_line.call_args_list:self.assertEqual(len(call.args),6)
        p.update();self.assertEqual(p.executor.submit.call_count,1)

    def test_cancel_edit_scope_close_timeout_and_late_result(self):
        for action in ('edit','clear','close','stream','session','connection','timeout'):
            p=panel();p.analyze();old=p.future;generation=p.generation;old.set_running_or_notify_cancel()
            if action=='edit':p.edited()
            if action in ('clear','close'):getattr(p,action)()
            if action=='stream':p.app.selected_stream_id=lambda:'other'
            if action=='session':p.app.session_id='other'
            if action=='connection':p.app.client=None
            if action=='timeout':p.deadline=monotonic()-1
            p.finish(generation);p.analyze();self.assertEqual(p.executor.submit.call_count,1)
            old.set_result(response());p.finish(generation);self.assertIsNone(p.data)

    def test_single_bin_draws_distinct_finite_markers(self):
        from math import isfinite
        for source, threshold in ((-50, -80), (-80, -80), (-1e308, 1e308), (1e308, -1e308), (0, 0)):
            with self.subTest(source=source, threshold=threshold):
                p=panel(); p.data=response(); p.data['points']=1
                p.data['frequencies_hz']=[100]
                p.data['sweep']['levels']=[source]; p.data['threshold_levels']=[threshold]
                p.draw()
                p.canvas.create_line.assert_not_called()
                p.canvas.create_oval.assert_called_once()
                p.canvas.create_rectangle.assert_called_once()
                self.assertEqual(p.canvas.create_oval.call_args.kwargs['fill'], '#6cd8ff')
                self.assertEqual(p.canvas.create_rectangle.call_args.kwargs['outline'], '#ffb454')
                for draw in (p.canvas.create_oval, p.canvas.create_rectangle):
                    self.assertEqual(len(draw.call_args.args), 4)
                    self.assertTrue(all(isfinite(v) for v in draw.call_args.args))
                    self.assertTrue(all(0 <= v <= 500 for v in draw.call_args.args))
                label=p.canvas.create_text.call_args.kwargs['text']
                self.assertIn('100 Hz', label)
                self.assertIn(f'исходный {source:g} dBm', label)
                self.assertIn(f'маска {threshold:g} dBm', label)
                if source == threshold:
                    dot=p.canvas.create_oval.call_args.args
                    square=p.canvas.create_rectangle.call_args.args
                    self.assertLess(square[0], dot[0]); self.assertLess(square[1], dot[1])
                    self.assertGreater(square[2], dot[2]); self.assertGreater(square[3], dot[3])

    def test_raw_draft_not_normalized_or_parsed_before_request(self):
        for raw in ('{invalid', '{"name":"a","name":"b"}'):
            p=panel();p.editor.get.return_value=raw;p.analyze()
            self.assertEqual(p.executor.submit.call_args.args[-1],raw)
        for raw in ('', ' ', 'é'*32769, '\ud800'):
            p=panel();p.editor.get.return_value=raw;p.analyze();p.executor.submit.assert_not_called()

    def test_isolated_failure(self):
        for exc in (PlatformClientError('old',status=404),PlatformClientError('bad',status=400),PlatformClientError('large',status=413),PlatformClientError('incompatible',status=409),RuntimeError('offline')):
            p=panel();p.analyze();p.future.set_exception(exc);p.finish(p.generation)
            self.assertIsNone(p.data);self.assertIn('не проверен',p.info.set.call_args.args[0])
        p=panel();p.analyze();bad=response();bad['mask']['name']='other';p.future.set_result(bad);p.finish(p.generation);self.assertIsNone(p.data)


class MaskHTTPTests(unittest.TestCase):
    def test_post_exact_raw(self):
        c=PlatformClient('http://localhost');c._request=Mock()
        raw=' {"duplicate":1,"duplicate":2} '
        c.sweep_mask_analysis('s/a','id?x',raw)
        c._request.assert_called_once_with('POST','/sessions/s%2Fa/sweep-mask-analysis',{'sweep_id':'id?x','mask_json':raw},expected_statuses=(200,))
        for raw in ('','é'*32769):
            with self.assertRaises(ValueError):c.sweep_mask_analysis('s','a',raw)
        with self.assertRaises(ValueError): c.sweep_mask_analysis('s','x'*524288,'{}')

    def test_200_only_and_response_bound(self):
        from radioinspector.contracts import api_payload
        from radioinspector.windows.client import MAX_RESPONSE_BYTES
        for status in (200,201,202,204,206):
            reply=Mock();reply.__enter__=Mock(return_value=reply);reply.__exit__=Mock(return_value=False)
            reply.status=status;reply.read.return_value=json.dumps(api_payload(response())).encode()
            opener=Mock();opener.open.return_value=reply
            c=PlatformClient('http://localhost',opener=opener)
            if status==200:self.assertEqual(c.sweep_mask_analysis('session','a','{}'),response())
            else:
                with self.assertRaises(PlatformClientError):c.sweep_mask_analysis('session','a','{}')
        reply.status=200;reply.read.return_value=b'x'*(MAX_RESPONSE_BYTES+1)
        with self.assertRaises(PlatformClientError) as caught:c.sweep_mask_analysis('session','a','{}')
        self.assertEqual(caught.exception.code,'response_too_large')
        self.assertEqual(reply.read.call_args.args,(MAX_RESPONSE_BYTES+1,))
