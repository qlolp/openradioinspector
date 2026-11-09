import io
import json
import unittest
from copy import deepcopy
from time import monotonic
from unittest.mock import Mock
from radioinspector.contracts import api_payload
from radioinspector.windows.client import PlatformClient, PlatformClientError
from radioinspector.windows.sweep_quality import SweepQualityPanel, validate_quality, validate_parameters, result_text
from tests.test_windows_sweep_threshold import panel as base_panel


def source():
    return dict(id='a',stream_id='s',level_unit='dBm',settings={'nullable':None,'sequence':False},frequencies_hz=[100,200,300],levels=[0,-20,-10],interval={'started_at':'2026-11-08T00:00:00.000001Z','ended_at':'2026-11-08T00:00:01Z'})


def response():
    s=source()
    return dict(session_id='session',stream_id='s',sweep={k:s[k] for k in ('id','level_unit','settings','interval')},parameters={'edge_margin_db':20,'high_level_dbm':0},points=3,peak_index=0,peak_frequency_hz=100,peak_level_dbm=0,high_level_count=1,edge_assessments=[dict(edge='first',index=0,frequency_hz=100,level_dbm=0,neighbor_index=1,neighbor_level_dbm=-20,delta_db=20,flagged=True),dict(edge='last',index=2,frequency_hz=300,level_dbm=-10,neighbor_index=1,neighbor_level_dbm=-20,delta_db=10,flagged=False)],flags=['first_edge_jump','level_threshold'],assessment='review',method='archived_edge_level_review_v1',hardware_diagnosis=False,raw_modified=False)


def panel():
    base=base_panel(); p=SweepQualityPanel.__new__(SweepQualityPanel);p.__dict__.update(base.__dict__)
    p.chosen_source=p.source=None
    for name in ('anchor','anchors','edge_margin','high_level'):setattr(p,name,Mock())
    p.anchor.get.return_value='a';p.edge_margin.get.return_value='20';p.high_level.get.return_value='0'
    p.app.snapshot.sweeps=[source()]
    return p


class QualityTests(unittest.TestCase):
    def test_validation_and_metadata(self):
        r=response();self.assertEqual(validate_quality(r,('session','a',20,0),source()),r)
        for settings in (None,[],{},['a',None,False],5):
            s=source();s['settings']=settings;r=response();r['sweep']['settings']=settings
            validate_quality(r,('session','a',20,0),s)
        self.assertIn('не диагностирует',result_text(response()))

    def test_core_boundary_reports(self):
        from datetime import datetime, timezone
        from radioinspector.contracts import Sweep, TimeInterval, to_primitive
        from radioinspector.sweep_quality import calculate_sweep_quality
        for points in (2, 16384):
            s=Sweep(id='a',session_id='session',stream_id='s',interval=TimeInterval(datetime.now(timezone.utc),datetime.now(timezone.utc)),frequencies_hz=list(range(points)),levels=[-20.0]*points,level_unit='dBm',settings={})
            data=calculate_sweep_quality('session',s,edge_margin_db=20,high_level_dbm=0)
            validate_quality(data,('session','a',20,0),to_primitive(s))
            self.assertEqual(data['assessment'],'no_flags')

    def test_standalone_sdk_partial_known_indices(self):
        from datetime import datetime, timezone
        from radioinspector.contracts import Sweep, TimeInterval
        from radioinspector.sweep_quality import calculate_sweep_quality
        def report(levels):
            sweep = Sweep(id='a', session_id='session', stream_id='s',
                interval=TimeInterval(datetime.now(timezone.utc), datetime.now(timezone.utc)),
                frequencies_hz=list(range(100, 100+len(levels))), levels=levels, level_unit='dBm', settings={})
            return calculate_sweep_quality('session', sweep, edge_margin_db=20, high_level_dbm=0)
        cases = []
        for points in (5, 8):
            equal = report([-20]*points)
            validate_quality(equal, ('session','a',20,0))
            wrong = deepcopy(equal); wrong['peak_index']=2; cases.append(wrong)
            wrong = deepcopy(equal); wrong.update(peak_index=2, peak_frequency_hz=102); cases.append(wrong)
            interior = report([-20,-20,0]+[-20]*(points-3))
            validate_quality(interior, ('session','a',20,0))
            for frequency in (100,101,100+points-1):
                wrong = deepcopy(interior); wrong['peak_frequency_hz']=frequency; cases.append(wrong)
            wrong = deepcopy(interior); wrong['high_level_count']=points-3; cases.append(wrong)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                validate_quality(data, ('session','a',20,0))
            raw = io.BytesIO(json.dumps(api_payload(data)).encode()); raw.status=200
            opener=Mock(); opener.open.return_value=raw
            with self.assertRaises(ValueError):
                PlatformClient('http://localhost:56039', opener=opener).sweep_quality('session','a')

    def test_invalid_parameters(self):
        for edge,high in [(0,0),(201,0),(True,0),(20,False),(20,-201),(20,101),(float('nan'),0),(20,float('inf'))]:
            with self.subTest(edge=edge,high=high),self.assertRaises(ValueError):validate_parameters(edge,high)
        validate_parameters(.01,-200);validate_parameters(200,100)

    def test_reject_inconsistent_response(self):
        changes=[lambda r:r.update(points=True),lambda r:r.update(points=1),lambda r:r.update(points=16385),lambda r:r.update(peak_index=True),lambda r:r.update(peak_frequency_hz=2**53),lambda r:r.update(peak_level_dbm=float('nan')),lambda r:r.update(high_level_count=True),lambda r:r.update(high_level_count=0),lambda r:r.update(hardware_diagnosis=0),lambda r:r.update(raw_modified=True),lambda r:r.update(flags=['level_threshold','first_edge_jump']),lambda r:r.update(assessment='no_flags'),lambda r:r.update(stream_id='other'),lambda r:r['sweep'].update(settings={'nullable':None,'sequence':0}),lambda r:r['parameters'].update(edge_margin_db=True),lambda r:r['edge_assessments'][0].update(flagged=1),lambda r:r['edge_assessments'][0].update(delta_db=21),lambda r:r['edge_assessments'][0].update(index=True)]
        for change in changes:
            r=response();change(r)
            with self.subTest(change=change),self.assertRaises(ValueError):validate_quality(r,('session','a',20,0),source())

    def test_unknown_report_fields_rejected(self):
        for path in ((), ('sweep',), ('sweep','interval'), ('parameters',), ('edge_assessments',0), ('edge_assessments',1)):
            r=response();target=r
            for key in path: target=target[key]
            target['unexpected']=None
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_quality(r,('session','a',20,0),source())

    def test_selected_frozen_and_scope(self):
        p=panel();p.update();p.analyze();p.executor.submit.assert_called_once_with(p.app.client.sweep_quality,'session','a',20.,0.)
        p.app.snapshot.sweeps=[];p.future.set_result(response());p.finish(p.generation)
        self.assertEqual(p.data,response());self.assertEqual(p.source,source())
        p.clear();self.assertEqual(p.chosen_source,source());p.analyze();self.assertEqual(p.executor.submit.call_count,2)

    def test_stale_completion_and_singleflight(self):
        for action in ('clear','close','session','stream','server','disconnect','timeout','threshold','selection'):
            p=panel();p.analyze();old=p.future;old.set_running_or_notify_cancel();g=p.generation
            if action in ('clear','close','disconnect'):getattr(p,action)()
            elif action=='session':p.app.session_id='other'
            elif action=='stream':p.app.selected_stream_id=lambda:'other'
            elif action=='server':p.app.client=Mock()
            elif action=='timeout':p.deadline=monotonic()-1
            elif action=='threshold':p.high_level.get.return_value='5';p.clear()
            elif action=='selection':p.anchor.get.return_value='other';p.choose_anchor()
            p.finish(g);p.analyze();self.assertEqual(p.executor.submit.call_count,1)
            old.set_result(response());p.finish(g);self.assertIsNone(p.data)

    def test_error_isolation(self):
        for status in (400,404,409,413,500):
            p=panel();p.analyze();p.future.set_exception(PlatformClientError('bad',status=status));p.finish(p.generation)
            self.assertIsNone(p.data);p.analyze();self.assertEqual(p.executor.submit.call_count,2)

    def test_exact_http_and_invalid_input(self):
        def client(raw=None,status=200):
            stream=io.BytesIO(raw if raw is not None else json.dumps(api_payload(response())).encode());stream.status=status
            opener=Mock();opener.open.return_value=stream
            return PlatformClient('http://localhost:56039',opener=opener),opener
        c,o=client();self.assertEqual(c.sweep_quality('session','a'),response())
        req=o.open.call_args.args[0];self.assertEqual(req.method,'POST');self.assertTrue(req.full_url.endswith('/sessions/session/sweep-quality'))
        self.assertEqual(json.loads(req.data),{'sweep_id':'a','edge_margin_db':20,'high_level_dbm':0});self.assertLessEqual(o.open.call_args.kwargs['timeout'],8)
        for edge,high in [(True,0),(0,0),(20,float('nan'))]:
            c,o=client()
            with self.assertRaises(ValueError):c.sweep_quality('session','a',edge,high)
            o.open.assert_not_called()
        for status in (201,202,204,400,404,409,413,500):
            c,o=client(status=status)
            with self.assertRaises(PlatformClientError):c.sweep_quality('session','a')
        for raw in (b'{"data":{},"data":{}}',b'{"value":NaN}',b'x'*2000001):
            c,o=client(raw)
            with self.assertRaises(PlatformClientError):c.sweep_quality('session','a')
