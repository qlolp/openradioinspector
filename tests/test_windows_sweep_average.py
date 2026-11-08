import io
import json
import os
import tempfile
import unittest
import urllib.error
from concurrent.futures import Future
from copy import deepcopy
from math import isfinite
from time import monotonic
from unittest.mock import Mock, patch
from tests.test_windows_sweep_threshold import panel as base_panel
from radioinspector.windows.sweep_average import (
    AveragePanel, validate_average, validate_change_detection, validate_change_parameters,
    validate_ids, source_text, save_average_change_file,
)
from radioinspector.windows.client import PlatformClient, PlatformClientError, MAX_RESPONSE_BYTES, AverageChangeResult
from radioinspector.contracts import api_payload


def sources():
    return [dict(id=str(i), stream_id='s', level_unit='dBm', frequencies_hz=[100,200,300], levels=[-50,-30,-50], settings={'rbw_hz':1000,'flags':[True], 'sequence': {'number':i, 'null':None}}, interval={'started_at':f'2026-11-08T00:00:0{i}Z', 'ended_at':f'2026-11-08T00:00:0{i+1}Z'}) for i in range(4)]


def response():
    sweeps=sources(); ref=sweeps[:3]; target=sweeps[-1]
    return dict(session_id='session',stream_id='s',frequencies_hz=[100,200,300],points=3,
        reference=dict(method='mean_power',count=3,source_ids=['0','1','2'],sources=[{k:s[k] for k in ('id','interval','settings')} for s in ref],interval={'started_at':ref[0]['interval']['started_at'],'ended_at':ref[-1]['interval']['ended_at']},levels=[-50,-30,-50],level_unit='dBm'),
        target={k:target[k] for k in ('id','interval','settings','levels','level_unit')},delta_levels=[0,0,0],delta_unit='dB',max_increase={'frequency_hz':100,'delta_db':0},min_change={'frequency_hz':100,'delta_db':0})


def detection_response():
    return {'comparison': response(), 'detection': {
        'minimum_increase_db': 6.0, 'minimum_bins': 2, 'regions': [], 'count': 0,
    }}


def region_fixture():
    sweeps = sources()
    deltas = [0, 6, 9, 9, 1, 7, 8, 0, 6]
    grid = [100, 200, 200, 300, 400, 500, 600, 700, 800]
    for source in sweeps: source.update(frequencies_hz=grid[:], levels=[-100] * len(grid))
    sweeps[-1]['levels'] = [-100 + delta for delta in deltas]
    result = detection_response(); comparison = result['comparison']
    comparison.update(points=len(grid), frequencies_hz=grid, delta_levels=deltas,
                      max_increase={'frequency_hz':200,'delta_db':9},
                      min_change={'frequency_hz':100,'delta_db':0})
    comparison['reference']['levels'] = [-100] * len(grid)
    comparison['target']['levels'] = sweeps[-1]['levels'][:]
    result['detection'].update(count=2, regions=[
        dict(start_index=1,end_index=3,start_frequency_hz=200,end_frequency_hz=300,
             peak_index=2,peak_frequency_hz=200,peak_increase_db=9,bin_count=3),
        dict(start_index=5,end_index=6,start_frequency_hz=500,end_frequency_hz=600,
             peak_index=6,peak_frequency_hz=600,peak_increase_db=8,bin_count=2),
    ])
    return result, sweeps


def panel():
    base=base_panel(); p=AveragePanel.__new__(AveragePanel); p.__dict__.update(base.__dict__)
    p.export_button=Mock();p.exporting=False;p.result_json=None
    p.sources=None;p.detection=None;p.count=Mock();p.count.get.return_value='3'
    p.threshold=Mock();p.threshold.get.return_value='6'
    p.minimum_bins=Mock();p.minimum_bins.get.return_value='2'
    p.app.snapshot.sweeps=sources()
    return p


class AverageValidationTests(unittest.TestCase):
    def check(self,r):return validate_average(r,('session','s'),sources()[:3],sources()[-1])
    def test_valid(self):
        self.assertEqual(self.check(response()),response())
        self.assertIn('N=3',source_text(sources()[:3]))
    def test_response_rejections(self):
        mutations=[lambda r:r.update(session_id='wrong'),lambda r:r.update(stream_id='wrong'),lambda r:r.update(points=True),lambda r:r.update(delta_unit='dBm'),lambda r:r.update(frequencies_hz=[100,200,301]),lambda r:r.update(frequencies_hz=[100.0,200,300]),lambda r:r.update(delta_levels=[1,0,0]),lambda r:r.update(delta_levels=[float('nan')]*3)]
        for key,value in [('method','mean_dbm'),('count',True),('count',2),('source_ids',['1','0','2']),('level_unit','S'),('levels',[-49,-30,-50]),('levels',[float('inf')]*3),('sources',[])]:
            mutations.append(lambda r,k=key,v=value:r['reference'].update({k:v}))
        mutations += [lambda r:r['reference']['sources'][0]['settings']['flags'].__setitem__(0,1),lambda r:r['reference']['sources'][0]['settings']['sequence'].update(number=9),lambda r:r['target']['settings']['sequence'].update(number=9),lambda r:r['target'].update(levels=[-50,-30,-49]),lambda r:r['reference']['interval'].update(ended_at='2026-11-08T00:00:04Z'),lambda r:r['max_increase'].update(frequency_hz=200),lambda r:r['min_change'].update(delta_db=True)]
        for mutation in mutations:
            r=deepcopy(response());mutation(r)
            with self.subTest(r=r),self.assertRaises(ValueError):self.check(r)
    def test_ids(self):
        for ids,target in [(['a'],'b'),(['a','a'],'b'),(['a','b'],'a'),(['a',' '],'b'),(['a','b'],'x'*129),(['a',True],'b'),(['a']*17,'b')]:
            with self.assertRaises(ValueError):validate_ids(ids,target)
    def test_one_bin_duplicate_grid_and_extreme(self):
        for grid in ([100],[100,100,100]):
            ss=sources();r=response();n=len(grid)
            for s in ss:s.update(frequencies_hz=grid,levels=[-1e308]*n)
            r.update(frequencies_hz=grid,points=n,delta_levels=[0]*n)
            r['reference']['levels']=[-1e308]*n;r['target']['levels']=[-1e308]*n
            validate_average(r,('session','s'),ss[:3],ss[-1])
    def test_source_validation(self):
        for key,value in [('frequencies_hz',[100,200,2**53]),('levels',[True]*3),('settings',{'sequence':1}),('settings',{'rbw':None}),('interval',{'started_at':'2026-11-08','ended_at':'2026-11-08'})]:
            ss=sources();ss[0][key]=value
            with self.assertRaises(ValueError):validate_average(response(),('session','s'),ss[:3],ss[-1])

    def test_utc_precision_and_provenance(self):
        for value in ('2026-11-08T00:00:00.0000009Z', '2026-11-08T00:00:00.000001Z',
                      '2026-11-08T00:00:00.0000000Z', '2026-11-08 00:00:00Z',
                      '2026-11-08T00:00:00+01:00', '2026-02-30T00:00:00Z'):
            r=response();r['reference']['interval']['started_at']=value
            with self.subTest(value=value),self.assertRaises(ValueError):self.check(r)
        for key in ('target','reference'):
            r=response()
            item=r['target'] if key=='target' else r['reference']['sources'][0]
            item['interval']['started_at']='2026-11-08T00:00:00.000001Z'
            with self.assertRaises(ValueError):self.check(r)
        r=response();r['reference']['interval']['started_at']='2026-11-08T00:00:00.000000+00:00'
        self.check(r)

    def test_adjacent_float_clamped_mean_accepted(self):
        ss=[]
        for i in range(10):
            source=deepcopy(sources()[0]);source['id']=str(i)
            source['levels']=[-2.591576022265827 if i<9 else -2.5915760222658264]*3
            ss.append(source)
        target=deepcopy(ss[0]);target['id']='target'
        r=response();r['reference'].update(count=10,source_ids=[s['id'] for s in ss],
            sources=[{k:s[k] for k in ('id','interval','settings')} for s in ss],
            interval=deepcopy(ss[0]['interval']),levels=deepcopy(ss[0]['levels']))
        r['target']={k:target[k] for k in ('id','interval','settings','levels','level_unit')}
        validate_average(r,('session','s'),ss,target)

    def test_change_detection_response_and_parameters(self):
        result = detection_response()
        self.assertEqual(validate_change_detection(
            result, ('session','s'), sources()[:3], sources()[-1], 6, 2,
        ), (response(), result['detection']))
        for threshold, bins in ((-1,2),(121,2),(float('nan'),2),(True,2),(6,0),(6,16385),(6,2.0)):
            with self.assertRaises(ValueError):validate_change_parameters(threshold,bins)
        bad = deepcopy(result); bad['detection']['count'] = 1
        with self.assertRaises(ValueError):
            validate_change_detection(bad,('session','s'),sources()[:3],sources()[-1],6,2)

    def test_regions_cover_entire_delta_and_preserve_first_peak_tie(self):
        result, sweeps = region_fixture()
        validate_change_detection(result,('session','s'),sweeps[:3],sweeps[-1],6,2)
        mutations = [
            lambda r:r['detection'].update(regions=r['detection']['regions'][1:],count=1),
            lambda r:r['detection'].update(regions=[],count=0),
            lambda r:r['detection']['regions'].reverse(),
            lambda r:r['detection']['regions'][0].update(start_index=2,bin_count=2),
            lambda r:r['detection']['regions'][0].update(peak_index=3,peak_frequency_hz=300),
            lambda r:r['detection']['regions'][0].update(start_index=True),
            lambda r:r['detection']['regions'][0].update(end_index=3.0),
            lambda r:r['detection']['regions'][0].update(start_frequency_hz=200.0),
            lambda r:r['detection']['regions'][0].update(bin_count=3.0),
            lambda r:r['detection']['regions'][0].update(peak_increase_db=float('nan')),
            lambda r:r['detection'].update(minimum_bins=2.0),
            lambda r:r['detection'].update(count=2.0),
            lambda r:r['detection'].update(extra=True),
            lambda r:r.update(extra=True),
        ]
        for mutate in mutations:
            bad = deepcopy(result); mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validate_change_detection(bad,('session','s'),sweeps[:3],sweeps[-1],6,2)

    def test_detection_boolean_aliases_are_never_numeric_values(self):
        result=detection_response()
        result['detection'].update(minimum_increase_db=0,minimum_bins=1,count=1,regions=[
            dict(start_index=0,end_index=2,start_frequency_hz=100,end_frequency_hz=300,
                 peak_index=0,peak_frequency_hz=100,peak_increase_db=0,bin_count=3),
        ])
        validate_change_detection(result,('session','s'),sources()[:3],sources()[-1],0,1)
        for key, value in (('minimum_increase_db',False),('minimum_bins',True),('count',True)):
            bad=deepcopy(result);bad['detection'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                validate_change_detection(bad,('session','s'),sources()[:3],sources()[-1],0,1)
        for key in ('start_index','peak_index','peak_increase_db'):
            bad=deepcopy(result);bad['detection']['regions'][0][key]=False
            with self.subTest(key=key),self.assertRaises(ValueError):
                validate_change_detection(bad,('session','s'),sources()[:3],sources()[-1],0,1)

    def test_detection_point_limit_and_duplicate_frequency_range(self):
        for n in (1,16384,16385):
            result=detection_response();comparison=result['comparison'];sweeps=sources()
            grid=[9_007_199_254_740_991]*n;levels=[-1e308]*n
            for source in sweeps:source.update(frequencies_hz=grid[:],levels=levels[:])
            comparison.update(points=n,frequencies_hz=grid,delta_levels=[0]*n,
                              max_increase={'frequency_hz':grid[0],'delta_db':0},
                              min_change={'frequency_hz':grid[0],'delta_db':0})
            comparison['reference']['levels']=levels[:];comparison['target']['levels']=levels[:]
            result['detection'].update(minimum_increase_db=0,minimum_bins=1,count=1,regions=[
                dict(start_index=0,end_index=n-1,start_frequency_hz=grid[0],end_frequency_hz=grid[-1],
                     peak_index=0,peak_frequency_hz=grid[0],peak_increase_db=0,bin_count=n),
            ])
            if n<=16384:validate_change_detection(result,('session','s'),sweeps[:3],sweeps[-1],0,1)
            else:
                with self.assertRaises(ValueError):validate_change_detection(result,('session','s'),sweeps[:3],sweeps[-1],0,1)

    def test_analysis_export_is_complete_atomic_and_create_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'analysis.json')
            save_average_change_file(path, response(), detection_response()['detection'])
            with open(path, encoding='utf-8') as file:
                artifact = json.load(file)
            self.assertEqual(artifact['format'], 'openradioinspector-sweep-average-change-detection')
            self.assertEqual(artifact['format_version'], 1)
            self.assertEqual(artifact['data']['comparison'], response())
            self.assertEqual(artifact['data']['detection'], detection_response()['detection'])
            with self.assertRaises(FileExistsError):
                save_average_change_file(path, {}, {}, overwrite=False)
            with open(path, encoding='utf-8') as file:
                self.assertEqual(json.load(file)['data']['comparison'], response())
            save_average_change_file(path, response(), detection_response()['detection'], overwrite=True)
            self.assertEqual(os.listdir(directory), ['analysis.json'])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'invalid.json')
            with self.assertRaises(ValueError):
                save_average_change_file(path, {'not_finite': float('nan')}, {})
            self.assertEqual(os.listdir(directory), [])

class AverageExportLifecycleTests(unittest.TestCase):
    def ready(self):
        p = panel(); p.pin(); p.compare()
        result = detection_response()
        result = AverageChangeResult(result, json.dumps(result))
        p.future.set_result(result); p.finish(p.generation)
        self.assertIsNotNone(p.data)
        return p, result

    def test_transport_to_file_preserves_exact_numeric_metadata(self):
        p = panel(); result = detection_response()
        settings = [source['settings'] for source in p.app.snapshot.sweeps]
        settings += [source['settings'] for source in result['comparison']['reference']['sources']]
        settings += [result['comparison']['target']['settings']]
        for item in settings:
            item.update({'é': 'composed', 'é': 'decomposed', '\ufeffkey': 'BOM'})
            item.update(precise=1.2345678901234567, large=9007199254740993,
                        text='"data": {"brace": "}"}', data={'comparison': {'points': 2.25}})
        data_json = json.dumps(result, ensure_ascii=False).replace('1.2345678901234567', '1.2345678901234567890123456789').replace('2.25', '2.25e0')
        wire = ('{"data": ' + data_json + ', "contract":"openradioinspector-platform","contract_version":1}').encode()
        opener = Mock(); reply = Mock(status=200)
        reply.__enter__ = Mock(return_value=reply); reply.__exit__ = Mock(return_value=False)
        reply.read.return_value = wire; opener.open.return_value = reply
        p.app.client = PlatformClient('http://localhost', opener=opener)
        p.pin(); p.compare()
        received = p.app.client.detect_average_sweep_changes('session', ['0','1','2'], '3', 6, 2)
        self.assertEqual(received.data_json, data_json)
        p.future.set_result(received); p.finish(p.generation)
        self.assertIsNotNone(p.data)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'analysis.json')
            with patch('tkinter.filedialog.asksaveasfilename', return_value=path): p.export_result()
            with open(path, encoding='utf-8') as file: saved = file.read()
            self.assertEqual(saved, '{"format":"openradioinspector-sweep-average-change-detection","format_version":1,"data":' + data_json + '}\n')
        # A parsed-only test double may still render, but cannot promise a lossless export.
        p.compare(); p.future.set_result(result); p.finish(p.generation)
        self.assertIsNotNone(p.data); self.assertIsNone(p.result_json)
        with patch('tkinter.filedialog.asksaveasfilename') as dialog: p.export_result()
        dialog.assert_not_called()

    def test_export_frozen_complete_and_reentry(self):
        p, result = self.ready()
        expected = deepcopy(result)
        result['comparison']['target']['settings']['large'] = 9007199254740993
        def choose(**kwargs):
            p.export_result()  # Native file dialogs run a nested Tk event loop.
            p.app.snapshot.sweeps = []
            return '/synthetic/analysis.json'
        with patch('tkinter.filedialog.asksaveasfilename', side_effect=choose) as dialog, \
             patch('os.path.lexists', return_value=False), \
             patch('radioinspector.windows.sweep_average.save_average_change_file') as save:
            p.export_result()
        dialog.assert_called_once()
        save.assert_called_once_with('/synthetic/analysis.json', expected['comparison'], expected['detection'], False, data_json=json.dumps(expected))
        self.assertFalse(p.exporting)

    def test_stale_callbacks_cannot_save(self):
        for stage in ('choose', 'overwrite'):
            for action in ('clear', 'parameters', 'pin', 'compare', 'server', 'session', 'stream', 'disconnect', 'close'):
                with self.subTest(stage=stage, action=action):
                    p, _ = self.ready()
                    def change(*args, **kwargs):
                        if action == 'parameters': p.parameters_changed()
                        elif action == 'server': p.app.client = Mock()
                        elif action == 'session': p.app.session_id = 'other'
                        elif action == 'stream': p.app.selected_stream_id = lambda: 'other'
                        elif action == 'disconnect': p.app.client = None
                        else: getattr(p, action)()
                        return '/synthetic/analysis.json' if stage == 'choose' else True
                    with patch('tkinter.filedialog.asksaveasfilename', side_effect=change if stage == 'choose' else None,
                               return_value='/synthetic/analysis.json'), \
                         patch('os.path.lexists', return_value=True), \
                         patch('tkinter.messagebox.askyesno', side_effect=change if stage == 'overwrite' else None,
                               return_value=True), \
                         patch('radioinspector.windows.sweep_average.save_average_change_file') as save:
                        p.export_result()
                    save.assert_not_called()
                    self.assertFalse(p.exporting)

    def test_cancel_decline_and_write_failure(self):
        for path, overwrite, approved, failure in [('', False, True, None), ('fixture.json', True, False, None),
                                                  ('fixture.json', True, True, OSError('disk full'))]:
            p, _ = self.ready()
            with patch('tkinter.filedialog.asksaveasfilename', return_value=path), \
                 patch('os.path.lexists', return_value=overwrite), \
                 patch('tkinter.messagebox.askyesno', return_value=approved), \
                 patch('radioinspector.windows.sweep_average.save_average_change_file', side_effect=failure) as save:
                p.export_result()
            self.assertFalse(p.exporting)
            if failure:
                save.assert_called_once(); self.assertIn('disk full', p.info.set.call_args.args[0])
            else: save.assert_not_called()
            self.assertIsNotNone(p.data)

    def test_dialog_tcl_errors_and_closed_panel_are_quiet(self):
        from tkinter import TclError
        for stage in ('choose', 'overwrite'):
            p, _ = self.ready()
            with patch('tkinter.filedialog.asksaveasfilename', return_value='fixture.json',
                       side_effect=TclError('root destroyed') if stage == 'choose' else None), \
                 patch('os.path.lexists', return_value=True), \
                 patch('tkinter.messagebox.askyesno', side_effect=TclError('root destroyed')), \
                 patch('radioinspector.windows.sweep_average.save_average_change_file') as save:
                p.export_result()
            save.assert_not_called(); self.assertFalse(p.exporting)
        p.close(); p.update = Mock(side_effect=AssertionError('closed widgets must not update'))
        p.export_result()

    def test_atomic_failure_preserves_destination_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'analysis.json')
            save_average_change_file(path, {'large': 9007199254740993, 'é': 'é'}, {})
            with open(path, 'rb') as file: original = file.read()
            self.assertIn(b'9007199254740993', original)
            for operation in ('fsync', 'replace'):
                with patch('radioinspector.windows.sweep_average.os.' + operation, side_effect=OSError('failure')):
                    with self.assertRaises(OSError): save_average_change_file(path, {}, {}, overwrite=True)
                with open(path, 'rb') as file: self.assertEqual(file.read(), original)
                self.assertEqual(os.listdir(directory), ['analysis.json'])


class AverageLifecycleTests(unittest.TestCase):
    def test_pin_explicit_freeze_no_auto_and_single_flight(self):
        p=panel();p.update();p.pin();p.executor.submit.assert_not_called()
        p.compare();p.compare();p.executor.submit.assert_called_once_with(
            p.app.client.detect_average_sweep_changes,'session',['0','1','2'],'3',6.0,2,
        )
        p.app.snapshot.sweeps[0]['settings']['sequence']['number']=999
        p.app.snapshot.sweeps=[];p.future.set_result(detection_response());p.finish(p.generation)
        self.assertEqual(p.data,response());self.assertEqual(p.sources[0]['settings']['sequence']['number'],0)
        self.assertEqual(p.canvas.create_line.call_count,3)
        for call in p.canvas.create_line.call_args_list:self.assertEqual(len(call.args),6)
        p.update();self.assertEqual(p.executor.submit.call_count,1)
        self.assertIn('Target 3',p.details.insert.call_args.args[1])
        self.assertIn('max_increase: +0 dB @ 100 Hz',p.details.insert.call_args.args[1])
        self.assertIn('min_change: +0 dB @ 100 Hz',p.details.insert.call_args.args[1])
    def test_exact_count_and_exclusion(self):
        for count in ('2','3'):
            p=panel();p.count.get.return_value=count;p.pin();self.assertEqual([s['id'] for s in p.sources],['1','2'] if count=='2' else ['0','1','2'])
        for count in ('4','1','17','3.0',' 3','３'):
            p=panel();p.count.get.return_value=count;p.pin();self.assertIsNone(p.sources);p.executor.submit.assert_not_called()
        p=panel();p.pin();p.app.snapshot.sweeps=sources()[:3];p.compare();p.executor.submit.assert_not_called()
    def test_clear_scope_timeout_close_retire_and_late_error(self):
        for action in ('clear','close','stream','session','connection','timeout'):
            p=panel();p.pin();p.compare();old=p.future;old.set_running_or_notify_cancel();generation=p.generation
            if action in ('clear','close'):getattr(p,action)()
            if action=='stream':p.app.selected_stream_id=lambda:'other'
            if action=='session':p.app.session_id='other'
            if action=='connection':p.app.client=None
            if action=='timeout':p.deadline=monotonic()-1
            p.finish(generation);p.compare();self.assertEqual(p.executor.submit.call_count,1)
            message=p.info.set.call_args;old.set_exception(RuntimeError('stale error'));p.finish(generation)
            self.assertEqual(p.info.set.call_args,message);self.assertIsNone(p.data)
    def test_repin_running_request_never_queues(self):
        p=panel();p.pin();p.compare();old=p.future;old.set_running_or_notify_cancel();p.pin();p.compare()
        self.assertEqual(p.executor.submit.call_count,1)
        old.set_result(detection_response());p.compare();self.assertEqual(p.executor.submit.call_count,2)
        p.future.set_result(detection_response());p.finish(p.generation);self.assertIsNotNone(p.data)
    def test_error_isolated_and_retry(self):
        for status in (400,404,409,413):
            p=panel();p.pin();p.compare();p.future.set_exception(PlatformClientError('bad',status=status));p.finish(p.generation)
            self.assertIsNone(p.data);self.assertIsNotNone(p.sources);p.compare();self.assertEqual(p.executor.submit.call_count,2)

    def test_parameter_edits_keep_frozen_sources_and_retire_running_result(self):
        for outcome in ('success','error'):
            p=panel();p.pin();p.compare();old=p.future;old.set_running_or_notify_cancel()
            generation=p.generation;frozen=deepcopy(p.sources)
            p.threshold.get.return_value='9';p.minimum_bins.get.return_value='1';p.parameters_changed()
            self.assertEqual(p.sources,frozen);self.assertIsNone(p.data);self.assertIsNone(p.detection)
            self.assertIn('N=3',p.details.insert.call_args.args[1]);message=p.info.set.call_args
            p.compare();self.assertEqual(p.executor.submit.call_count,1)
            if outcome=='success':old.set_result(detection_response())
            else:old.set_exception(RuntimeError('stale failure'))
            message=p.info.set.call_args;p.finish(generation);self.assertEqual(p.info.set.call_args,message)
            p.compare();self.assertEqual(p.executor.submit.call_count,2)
            p.executor.submit.assert_called_with(p.app.client.detect_average_sweep_changes,'session',['0','1','2'],'3',9.0,1)
            result=detection_response();result['detection'].update(minimum_increase_db=9.0,minimum_bins=1)
            p.future.set_result(result);p.finish(p.generation);self.assertIsNotNone(p.data)
            p.parameters_changed();self.assertEqual(p.sources,frozen)
            self.assertIsNone(p.data);self.assertIsNone(p.detection)

    def test_failed_rerun_removes_old_detection(self):
        p=panel();p.pin();p.compare();p.future.set_result(detection_response());p.finish(p.generation)
        self.assertIsNotNone(p.detection)
        p.compare();self.assertIsNone(p.detection)
        p.future.set_exception(PlatformClientError('offline'));p.finish(p.generation)
        self.assertIsNone(p.data);self.assertIsNone(p.detection)
    def test_full_grid_draw_and_finite_markers(self):
        for n in (1,3,16384):
            p=panel();p.data=response();p.data.update(points=n,frequencies_hz=[100]*n,delta_levels=[0]*n)
            p.data['target']['levels']=[1e308]*n;p.data['reference']['levels']=[-1e308]*n;p.draw()
            if n==1:
                self.assertEqual(p.canvas.create_oval.call_count,2);self.assertEqual(p.canvas.create_rectangle.call_count,1)
                calls=p.canvas.create_oval.call_args_list+p.canvas.create_rectangle.call_args_list
            else:
                calls=p.canvas.create_line.call_args_list;self.assertEqual(len(calls),3)
                for call in calls:self.assertEqual(len(call.args),n*2)
            for call in calls:self.assertTrue(all(isfinite(v) for v in call.args))

    def test_change_regions_are_highlighted_on_delta_chart(self):
        p=panel();p.data=response()
        p.detection={'regions':[{
            'start_index':1,'end_index':2,'start_frequency_hz':200,'end_frequency_hz':300,
            'peak_index':2,'peak_frequency_hz':300,'peak_increase_db':8,'bin_count':2,
        }]}
        p.draw()
        highlights=[call for call in p.canvas.create_rectangle.call_args_list
                    if call.kwargs.get('fill')=='#285538']
        self.assertEqual(len(highlights),1)
        self.assertEqual(highlights[0].kwargs['stipple'],'gray50')

    def test_actual_refresh_error_invalidates_running_and_completed_state(self):
        import queue
        from radioinspector.windows.app import WindowsApp
        for running in (False,True):
            p=panel();p.pin()
            if running:
                p.compare();old=p.future;old.set_running_or_notify_cancel();generation=p.generation
            else:p.data=response()
            app=WindowsApp.__new__(WindowsApp)
            app._closed=False;app._revision=7;app._messages=queue.Queue()
            app.root=Mock();app.connection_var=Mock();app._show_archive_capacity=Mock();app._render_controls=Mock()
            app.average_panel=p;app._refresh_running=True
            failed=Future();failed.set_exception(PlatformClientError('offline',code='connection_error'))
            app._messages.put(('snapshot',failed,7,''));app._drain_messages()
            self.assertIsNone(p.sources);self.assertIsNone(p.data)
            if running:
                old.set_result(detection_response());p.finish(generation);self.assertIsNone(p.data)

    def test_duplicate_equal_bins_have_distinct_visible_markers(self):
        p=panel();p.data=response();p.data['frequencies_hz']=[100]*3
        p.data['target']['levels']=[-50]*3;p.data['reference']['levels']=[-50]*3
        p.draw();self.assertEqual(p.canvas.create_line.call_count,3)
        self.assertEqual(p.canvas.create_oval.call_count,2);p.canvas.create_rectangle.assert_called_once()
        circle=p.canvas.create_oval.call_args_list[0];square=p.canvas.create_rectangle.call_args
        self.assertEqual(circle.kwargs['fill'],'#6cd8ff');self.assertEqual(square.kwargs['outline'],'#ffb454')
        self.assertLess(square.args[0],circle.args[0]);self.assertGreater(square.args[2],circle.args[2])


class AverageHTTPTests(unittest.TestCase):
    def test_exact_post_and_validation(self):
        c=PlatformClient('http://localhost');c._request=Mock();c.compare_average_sweeps('s/a',['a','b'],'c')
        c._request.assert_called_once_with('POST','/sessions/s%2Fa/sweep-average-comparison',{'reference_ids':['a','b'],'target_id':'c'},expected_statuses=(200,))
        with self.assertRaises(ValueError):c.compare_average_sweeps('s',['a','b'],'a')
    def test_exact_change_detection_post_and_parameters(self):
        c=PlatformClient('http://localhost');c._request=Mock()
        c.detect_average_sweep_changes('s/a',['a','b'],'c',6,2)
        c._request.assert_called_once_with(
            'POST','/sessions/s%2Fa/sweep-average-change-detection',
            {'reference_ids':['a','b'],'target_id':'c','minimum_increase_db':6.0,'minimum_bins':2},
            expected_statuses=(200,),strict_json=True,timeout=8.0,exact_envelope=True,preserve_data_json=True,
        )
        for threshold, bins in ((-1,2),(121,2),(float('nan'),2),(True,2),(6,0),(6,16385),(6,2.0)):
            with self.assertRaises(ValueError):
                c.detect_average_sweep_changes('s',['a','b'],'c',threshold,bins)
    def test_unicode_codepoints_and_utf8_body_bound(self):
        c=PlatformClient('http://localhost'); c._request=Mock()
        c.compare_average_sweeps('s',['😀'*128,'b'],'c')
        for ids in (['😀'*129,'b'], ['a','\ud800'], [('😀'*127)+str(i) for i in range(16)]):
            with self.assertRaises(ValueError): c.compare_average_sweeps('s',ids,'c')
        self.assertEqual(c._request.call_count,1)

    def test_200_only_bounded_all_statuses(self):
        for status in (200,201,202,204,206,400,404,409,413,500):
            for large in (False,True):
                raw=b'x'*(MAX_RESPONSE_BYTES+1) if large else json.dumps(api_payload(response())).encode()
                opener=Mock();reply=Mock();reply.__enter__=Mock(return_value=reply);reply.__exit__=Mock(return_value=False);reply.status=status;reply.read.return_value=raw
                if status>=400:opener.open.side_effect=urllib.error.HTTPError('http://localhost',status,'error',{},io.BytesIO(raw))
                else:opener.open.return_value=reply
                c=PlatformClient('http://localhost',opener=opener)
                if status==200 and not large:self.assertEqual(c.compare_average_sweeps('session',['0','1','2'],'3'),response())
                else:
                    with self.assertRaises(PlatformClientError):c.compare_average_sweeps('session',['0','1','2'],'3')
                if status<400:reply.read.assert_called_once_with(MAX_RESPONSE_BYTES+1)

    def test_change_transport_200_only_bounded_before_decode_and_strict_envelope(self):
        def client(raw,status=200,timeout=120):
            opener=Mock();reply=Mock();reply.__enter__=Mock(return_value=reply)
            reply.__exit__=Mock(return_value=False);reply.status=status;reply.read.return_value=raw
            if status>=400:opener.open.side_effect=urllib.error.HTTPError('http://localhost',status,'error',{},io.BytesIO(raw))
            else:opener.open.return_value=reply
            return PlatformClient('http://localhost',timeout=timeout,opener=opener),opener,reply
        def request(c):return c.detect_average_sweep_changes('session',['0','1','2'],'3',6,2)
        for status in (200,201,202,204,206,400,404,409,413,500):
            for large in (False,True):
                # Invalid UTF-8 makes any attempt to decode an oversized body observable.
                raw=b'\xff'*(MAX_RESPONSE_BYTES+1) if large else json.dumps(api_payload(detection_response())).encode()
                c,opener,reply=client(raw,status)
                if status==200 and not large:self.assertEqual(request(c),detection_response())
                else:
                    with self.assertRaises(PlatformClientError) as error:request(c)
                    if large:self.assertEqual(error.exception.code,'response_too_large')
                    elif status<400:self.assertEqual(error.exception.code,'unexpected_status')
                if status<400:reply.read.assert_called_once_with(MAX_RESPONSE_BYTES+1)
                self.assertEqual(opener.open.call_args.kwargs['timeout'],8.0)
        for envelope in (dict(api_payload({}),extra=True),dict(api_payload({}),error={}),
                         dict(api_payload({}),contract_version=True),{'contract':'openradioinspector-platform','contract_version':1}):
            with self.subTest(envelope=envelope),self.assertRaises(PlatformClientError):
                request(client(json.dumps(envelope).encode())[0])
        for raw in (b'{"data":{},"data":{}}',b'{"data":{"nested":NaN}}',b'\xff'):
            with self.subTest(raw=raw),self.assertRaises(PlatformClientError) as error:request(client(raw)[0])
            self.assertEqual(error.exception.code,'invalid_json')
        c,opener,_=client(json.dumps(api_payload(detection_response())).encode(),timeout=.5);request(c)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],.5)

    def test_detection_unicode_ids_and_request_byte_bound(self):
        c=PlatformClient('http://localhost');c._request=Mock()
        c.detect_average_sweep_changes('s/😀',['😀'*128,' b '],' c ',0,1)
        for ids in (['😀'*129,'b'],['a','\ud800'],[('😀'*127)+str(i) for i in range(16)]):
            with self.subTest(ids=ids),self.assertRaises(ValueError):c.detect_average_sweep_changes('s',ids,'c',6,2)
        for session in ('',' ','\ud800',True):
            with self.subTest(session=session),self.assertRaises(ValueError):c.detect_average_sweep_changes(session,['a','b'],'c',6,2)
        self.assertEqual(c._request.call_count,1)

    def test_detection_exact_utf8_request_byte_boundary(self):
        ids=[str(i) for i in range(17)]
        def body_size():
            return len(json.dumps({'reference_ids':ids[:-1],'target_id':ids[-1],
                'minimum_increase_db':6.0,'minimum_bins':2},ensure_ascii=False).encode('utf-8'))
        for i in range(17):
            while len(ids[i])<128 and body_size()+6<=8192:ids[i]+='\u0001'
        for i in range(17):
            while len(ids[i])<128 and body_size()<8192:ids[i]+='x'
        self.assertEqual(body_size(),8192)
        c=PlatformClient('http://localhost');c._request=Mock()
        c.detect_average_sweep_changes('s',ids[:-1],ids[-1],6,2)
        for i in range(17):
            if len(ids[i])<128:ids[i]+='x';break
        self.assertEqual(body_size(),8193)
        with self.assertRaises(ValueError):c.detect_average_sweep_changes('s',ids[:-1],ids[-1],6,2)
        self.assertEqual(c._request.call_count,1)


class AverageActualTkHTTPTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('ORI_AVERAGE_DETECTION_SMOKE_BASE_URL'), 'Explicit Tk desktop opt-in required')
    def test_hidden_root_destroyed_inside_export_dialog(self):
        import tkinter as tk
        from tkinter import ttk
        from types import SimpleNamespace
        for stage in ('choose', 'overwrite'):
            root = tk.Tk(); root.withdraw()
            app = SimpleNamespace(tk=tk, ttk=ttk, root=root, client=Mock(), session_id='session',
                selected_stream_id=lambda: 's', snapshot=SimpleNamespace(
                    session={'id': 'session'}, spectrum_stream_id='s', sweeps=sources()))
            notebook = ttk.Notebook(root); notebook.pack(); p = AveragePanel(app, notebook)
            p.update(); result = detection_response()
            p.data = result['comparison']; p.detection = result['detection']; p.result_json = json.dumps(result)
            def destroy(*args, **kwargs):
                def close(): p.close(); root.destroy()
                root.after(0, close); root.update()
                raise tk.TclError('dialog parent destroyed')
            with patch('tkinter.filedialog.asksaveasfilename', return_value='fixture.json',
                       side_effect=destroy if stage == 'choose' else None), \
                 patch('os.path.lexists', return_value=True), \
                 patch('tkinter.messagebox.askyesno', side_effect=destroy), \
                 patch('radioinspector.windows.sweep_average.save_average_change_file') as save:
                p.export_result()
            save.assert_not_called(); self.assertTrue(p.closed); self.assertFalse(p.exporting)
            p.export_result()  # No access to destroyed widgets.

    @unittest.skipUnless(os.environ.get('ORI_AVERAGE_SMOKE_BASE_URL'), 'Explicit synthetic fixture and Tk desktop required')
    def test_hidden_panel_detection_parameters_and_original_comparison(self):
        import tkinter as tk
        from tkinter import ttk
        from time import sleep
        from types import SimpleNamespace
        root=tk.Tk();root.withdraw()
        client=PlatformClient(os.environ['ORI_AVERAGE_SMOKE_BASE_URL'])
        sweeps=client.list_sweeps('average-one','spectrum-one')
        app=SimpleNamespace(tk=tk,ttk=ttk,root=root,client=client,session_id='average-one',
            selected_stream_id=lambda:'spectrum-one',snapshot=SimpleNamespace(
                session={'id':'average-one'},spectrum_stream_id='spectrum-one',sweeps=sweeps))
        notebook=ttk.Notebook(root);notebook.pack();p=AveragePanel(app,notebook)
        def finish():
            until=monotonic()+10
            while p.future is not None and monotonic()<until:root.update();sleep(.02)
            self.assertIsNotNone(p.data,p.info.get())
        try:
            p.pin();frozen=deepcopy(p.sources);self.assertIsNone(p.future)
            p.compare();finish();self.assertEqual(p.detection['count'],0)
            original=client.compare_average_sweeps('average-one',[s['id'] for s in frozen],sweeps[-1]['id'])
            self.assertEqual(p.data,original)
            p.minimum_bins.set('1');self.assertEqual(p.sources,frozen)
            self.assertIsNone(p.data);self.assertIsNone(p.detection);self.assertIsNone(p.future)
            p.compare();finish();self.assertEqual(p.detection['count'],1)
            region=p.detection['regions'][0]
            self.assertEqual((region['start_index'],region['end_index'],region['peak_index']),(30,30,30))
            self.assertEqual(region['peak_frequency_hz'],103_000_000)
            self.assertAlmostEqual(region['peak_increase_db'],20)
            lines=[i for i in p.canvas.find_all() if p.canvas.type(i)=='line']
            self.assertEqual(len(lines),3)
            for line in lines:self.assertEqual(len(p.canvas.coords(line)),202)
            highlights=[i for i in p.canvas.find_all() if p.canvas.type(i)=='rectangle' and p.canvas.itemcget(i,'fill')=='#285538']
            self.assertEqual(len(highlights),1)
            p.threshold.set('21');p.compare();finish();self.assertEqual(p.detection['count'],0)
            self.assertEqual(p.sources,frozen)
            app.session_id='average-two';p.update();self.assertIsNone(p.sources);self.assertIsNone(p.data)
        finally:p.close();root.destroy()

    @unittest.skipUnless(os.environ.get('ORI_AVERAGE_DETECTION_SMOKE_BASE_URL'), 'Explicit synthetic detection fixture and Tk desktop required')
    def test_hidden_live_detection_regions_and_parameter_traces(self):
        import tkinter as tk
        from tkinter import ttk
        from time import sleep
        from types import SimpleNamespace
        root=tk.Tk();root.withdraw()
        client=PlatformClient(os.environ['ORI_AVERAGE_DETECTION_SMOKE_BASE_URL'])
        sweeps=client.list_sweeps('changes-one','spectrum-one')
        app=SimpleNamespace(tk=tk,ttk=ttk,root=root,client=client,session_id='changes-one',
            selected_stream_id=lambda:'spectrum-one',snapshot=SimpleNamespace(
                session={'id':'changes-one'},spectrum_stream_id='spectrum-one',sweeps=sweeps))
        notebook=ttk.Notebook(root);notebook.pack();p=AveragePanel(app,notebook)
        def finish():
            until=monotonic()+10
            while p.future is not None and monotonic()<until:root.update();sleep(.02)
            self.assertIsNotNone(p.data,p.info.get())
        try:
            p.pin();frozen=deepcopy(p.sources);self.assertIsNone(p.future)
            p.compare();finish()
            self.assertEqual(p.data,client.compare_average_sweeps('changes-one',[s['id'] for s in frozen],sweeps[-1]['id']))
            self.assertEqual([(r['start_index'],r['end_index'],r['peak_index']) for r in p.detection['regions']],[(30,33,31),(69,70,70)])
            self.assertEqual([r['peak_increase_db'] for r in p.detection['regions']],[12,9])
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, 'analysis.json')
                def choose(**kwargs):
                    root.update(); p.export_result()
                    return path
                with patch('tkinter.filedialog.asksaveasfilename', side_effect=choose) as dialog:
                    p.export_result()
                dialog.assert_called_once()
                with open(path, encoding='utf-8') as file: artifact = json.load(file)
                self.assertEqual(artifact['data'], {'comparison': p.data, 'detection': p.detection})
                with open(path, 'rb') as file: saved = file.read()
                def overwrite(*args, **kwargs):
                    p.threshold.set('120'); root.update(); return True
                with patch('tkinter.filedialog.asksaveasfilename', return_value=path), \
                     patch('tkinter.messagebox.askyesno', side_effect=overwrite):
                    p.export_result()
                with open(path, 'rb') as file: self.assertEqual(file.read(), saved)
                p.threshold.set('6'); p.compare(); finish()

            lines=[i for i in p.canvas.find_all() if p.canvas.type(i)=='line'];self.assertEqual(len(lines),3)
            for line in lines:self.assertEqual(len(p.canvas.coords(line)),202)
            highlights=[i for i in p.canvas.find_all() if p.canvas.type(i)=='rectangle' and p.canvas.itemcget(i,'fill')=='#285538']
            self.assertEqual(len(highlights),2)
            p.threshold.set('12');self.assertEqual(p.sources,frozen);self.assertIsNone(p.data);self.assertIsNone(p.detection)
            p.compare();finish()
            self.assertEqual([(r['start_index'],r['end_index'],r['peak_index']) for r in p.detection['regions']],[(31,32,31)])
            p.minimum_bins.set('1');self.assertEqual(p.sources,frozen);self.assertIsNone(p.data)
            p.compare();finish();self.assertEqual(p.detection['count'],2)
            self.assertEqual(p.detection['regions'][1]['start_index'],90)
            # The Tk variable trace invalidates even a completed-but-not-yet-delivered future.
            p.compare();old=p.future;generation=p.generation;p.threshold.set('120')
            until=monotonic()+10
            while not old.done() and monotonic()<until:root.update();sleep(.02)
            p.finish(generation);self.assertIsNone(p.data);self.assertIsNone(p.detection);self.assertEqual(p.sources,frozen)
            p.compare();finish();self.assertEqual(p.detection['count'],0)
            app.session_id='changes-two';p.update();self.assertIsNone(p.sources);self.assertIsNone(p.data)
        finally:p.close();root.destroy()
