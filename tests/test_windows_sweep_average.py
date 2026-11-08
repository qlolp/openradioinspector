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
from unittest.mock import Mock
from tests.test_windows_sweep_threshold import panel as base_panel
from radioinspector.windows.sweep_average import (
    AveragePanel, validate_average, validate_change_detection, validate_change_parameters,
    validate_ids, source_text, save_average_change_file,
)
from radioinspector.windows.client import PlatformClient, PlatformClientError, MAX_RESPONSE_BYTES
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


def panel():
    base=base_panel(); p=AveragePanel.__new__(AveragePanel); p.__dict__.update(base.__dict__)
    p.sources=None;p.detection=None;p.count=Mock();p.count.get.return_value='3'
    p.export_button=Mock()
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
            expected_statuses=(200,),
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
