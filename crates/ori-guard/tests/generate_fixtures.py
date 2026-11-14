"""Emit exact Python monitoring-domain fixtures without ports, HTTP or hardware."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from datetime import datetime,timedelta,timezone
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from radioinspector.contracts import Sweep,TimeInterval,to_primitive
from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.monitoring.service import MonitoringService
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore

STAMP=datetime(2026,11,11,13,0,0,123456,tzinfo=timezone.utc)
START=STAMP-timedelta(hours=1)
SESSION='guard-сессия/%+?'
ANALYZER='spectrum/one'
RECEIVER='receiver+one'


def emit():
    counter=0
    def identity():
        nonlocal counter
        counter+=1
        return SimpleNamespace(hex=f'{counter:032x}')
    with tempfile.TemporaryDirectory(prefix='ori-python-guard-fixture-') as directory:
        service=SessionService(store=SQLiteSessionStore(str(Path(directory)/'archive.sqlite')),clock=lambda:STAMP)
        workers=DeviceWorkerManager(service)
        monitoring=MonitoringService(service,workers)
        inputs=[]
        try:
            session=service.create_session(dict(id=SESSION,name='Synthetic guard session',streams=[
                dict(id=ANALYZER,device='tinySA Ultra',role='spectrum_analyzer',level_unit='dBm',port='sim://tinysa',
                     capabilities=['sweep','waterfall'],settings=dict(adapter='tinysa-sim',start_hz=100000000,stop_hz=100080000,points=9,period_s=0.01)),
                dict(id=RECEIVER,device='AR-DV1',role='receiver',level_unit='s_meter_count',port='sim://ardv1',
                     capabilities=['tune','audio'],settings=dict(adapter='ardv1-sim',frequency_hz=145500000,mode='FM',period_s=0.01))]))
            def ingest(index,level=None):
                levels=[-100.0]*9
                if level is not None: levels[4]=level
                started=START+timedelta(seconds=4*index)
                value=Sweep(id=f'sweep-α/%+{index:03d}',session_id=SESSION,stream_id=ANALYZER,
                    interval=TimeInterval(started,started+timedelta(seconds=1)),frequencies_hz=tuple(100000000+10000*i for i in range(9)),
                    levels=tuple(levels),level_unit='dBm',settings={'adapter':'synthetic','rbw_hz':10000,'sequence':index,'operator_metadata':{'é':'raw','é':'distinct','huge':2**80,'reserved':{'$serde_json::private::Number':'source'},'negative_zero':-0.0}})
                service.ingest_sweep(value)
                inputs.append(to_primitive(value))
            with patch('radioinspector.monitoring.models.uuid4',identity),patch('radioinspector.monitoring.models.utc_now',lambda:STAMP),patch('radioinspector.monitoring.preparation.utc_now',lambda:STAMP):
                project=monitoring.create(dict(name='Room α',site='Cité+%/?',session_id=SESSION))
                created=to_primitive(project)
                preparation=monitoring.save_preparation(project['id'],dict(antenna_notes='RX fixture',receive_path_confirmed=True,location_confirmed=True))
                allowed=monitoring.add_allowed(project['id'],dict(name='Known carrier',start_hz=100060000,end_hz=100070000,comment='Original units',watch_changes=True))
                for index in range(20): ingest(index)
                baseline=monitoring.create_baseline(project['id'],dict(name='Quiet',location='Roof',confirm=True))
                prepared=monitoring.project(project['id'])
                for index in range(20,23): ingest(index,-92.0)
                first=monitoring.analyze(project['id'])
                event=monitoring.store.list('event',project['id'])[0]
                reviewed=monitoring.review_event(project['id'],event['id'],dict(disposition='false_positive',comment='Known fixture'))
                for index in range(23,28): ingest(index)
                quiet=monitoring.analyze(project['id'])
                for index in range(40,43): ingest(index,-92.0)
                gap=monitoring.analyze(project['id'])
                replay=monitoring.analyze(project['id'])
                history=monitoring.history(project['id'],{'limit':['3'],'min_hz':['100020000'],'max_hz':['100060000']})
                history_tail=monitoring.history(project['id'],{'limit':['3'],'min_hz':['100020000'],'max_hz':['100060000'],'after':[history['next_cursor']]})
                comparison=monitoring.history_comparison(project['id'],inputs[-1]['id'])
                records={kind:monitoring.store.list(kind,project['id'],limit=1000) for kind in ('project','baseline','allowed','event','event-source','quality')}
            return dict(fixture_version=1,synthetic=True,hardware_validation='not_run',clock=STAMP.isoformat().replace('+00:00','Z'),
                session=to_primitive(session),sweeps=inputs,created=created,preparation=preparation,allowed=allowed,baseline=baseline,
                prepared_project=prepared,first_analysis=first,first_event=event,reviewed_event=reviewed,quiet_analysis=quiet,
                gap_analysis=gap,replay_analysis=replay,history=history,history_tail=history_tail,comparison=comparison,records=records)
        finally:
            monitoring.close();workers.close();service.close()


def emit_operations():
    from contextlib import ExitStack
    from urllib.error import URLError
    class Reply:
        status=204
        def __enter__(self):return self
        def __exit__(self,*_):return False
    class Transport:
        fail=True
        requests=[]
        def open(self,request,timeout):
            self.requests.append(dict(url=request.full_url,body=request.data.decode(),headers=dict(request.header_items()),timeout=timeout))
            if self.fail:raise URLError('synthetic unavailable')
            return Reply()
    with tempfile.TemporaryDirectory(prefix='ori-python-guard-operations-') as directory:
        service=SessionService(store=SQLiteSessionStore(str(Path(directory)/'archive.sqlite')),clock=lambda:STAMP)
        workers=DeviceWorkerManager(service);monitoring=MonitoringService(service,workers)
        transport=Transport();monitoring._delivery_opener=transport
        try:
            service.create_session(dict(id=SESSION,name='Synthetic guard session',streams=[
                dict(id=ANALYZER,device='tinySA Ultra',role='spectrum_analyzer',level_unit='dBm',port='sim://tinysa',
                     capabilities=['sweep','waterfall'],settings=dict(adapter='tinysa-sim',start_hz=100000000,stop_hz=100080000,points=9,period_s=0.01)),
                dict(id=RECEIVER,device='AR-DV1',role='receiver',level_unit='s_meter_count',port='sim://ardv1',
                     capabilities=['tune','audio'],settings=dict(adapter='ardv1-sim',frequency_hz=145500000,mode='FM',period_s=0.01))]))
            with ExitStack() as patches:
                patches.enter_context(patch('radioinspector.monitoring.models.uuid4',lambda:SimpleNamespace(hex='0'*31+'1')))
                patches.enter_context(patch('radioinspector.monitoring.models.utc_now',lambda:STAMP))
                patches.enter_context(patch('radioinspector.monitoring.operations.utc_now',lambda:STAMP))
                project=monitoring.create(dict(name='Operations',site='Fixture',session_id=SESSION));identifier=project['id']
                defaults=monitoring.settings(identifier)
                update=dict(autostart=True,archive=dict(minimum_free_bytes=1048576,minimum_retention_seconds=60),
                    detector=dict(margin_db=8,impulse_db=16,hysteresis_db=4,confirmations=2,window_sweeps=4,dwell_seconds=1),
                    webhook=dict(enabled=True,url='https://invalid.example/notify',secret='public-synthetic-fixture-key'))
                settings=monitoring.update_settings(identifier,update)
                configured=monitoring.configure(identifier,dict(receiver_mode='FM',audio=None))
                event=dict(id='event/%+α',kind='persistent',center_frequency_hz=145500005)
                notification=monitoring.notify_event(monitoring.project(identifier),event)
                monitoring.notify_event(monitoring.project(identifier),event)
                pending=monitoring.alerts(identifier,{})
                first=monitoring.deliver_one(STAMP);failed=monitoring.alerts(identifier,{})
                early=monitoring.deliver_one(STAMP+timedelta(seconds=9))
                transport.fail=False
                second=monitoring.deliver_one(STAMP+timedelta(seconds=10));sent=monitoring.alerts(identifier,{})
                acknowledged=monitoring.acknowledge_alert(identifier,notification,{})
                alerts=monitoring.alerts(identifier,{})
                return dict(fixture_version=1,synthetic=True,hardware_validation='not_run',clock=STAMP.isoformat().replace('+00:00','Z'),
                    defaults=defaults,update=update,settings=settings,configured=configured,event=event,notification_id=notification,
                    pending=pending,first_delivery=first,failed=failed,early_delivery=early,second_delivery=second,sent=sent,
                    acknowledged=acknowledged,alerts=alerts,transport_requests=transport.requests)
        finally:monitoring.close();workers.close();service.close()


def emit_serial():
    import copy
    from radioinspector.tinysa.commands import CONTROL_PROFILE
    cases=[]
    for changed in (False,True):
        base=emit();session=base['session'];streams=copy.deepcopy(session['streams'])
        streams[0]['port']='COM3';streams[0]['settings']['adapter']='tinysa-serial'
        streams[1]['port']='COM4';streams[1]['settings']['adapter']='ardv1-serial'
        context=dict(profile=CONTROL_PROFILE,settings=dict(start_hz=100000000,stop_hz=100080000,points=9,rbw_hz=10000,
            attenuation_db='auto',lna=changed,ultra=False,calculation='off'),device_mode='input',device_mode_readback=False,
            settings_source='command_acknowledged',settings_readback=False,settings_complete=False,hardware_validation='not_run')
        with tempfile.TemporaryDirectory(prefix='ori-python-guard-serial-') as directory:
            service=SessionService(store=SQLiteSessionStore(str(Path(directory)/'archive.sqlite')),clock=lambda:STAMP)
            workers=DeviceWorkerManager(service);monitoring=MonitoringService(service,workers)
            try:
                service.create_session(dict(id=SESSION,name=session['name'],streams=streams))
                with patch('radioinspector.monitoring.models.uuid4',lambda:SimpleNamespace(hex='0'*31+'1')),patch('radioinspector.monitoring.models.utc_now',lambda:STAMP),patch('radioinspector.monitoring.preparation.utc_now',lambda:STAMP),patch.object(monitoring,'storage_report',return_value={'ready':True}):
                    project=monitoring.create(dict(name='Serial policy',site='Synthetic only',session_id=SESSION));identifier=project['id']
                    undeclared=monitoring.preflight(identifier)
                    monitoring.save_preparation(identifier,dict(antenna_notes='Synthetic RX',receive_path_confirmed=True,location_confirmed=True))
                    inputs=copy.deepcopy(base['sweeps'][:20])
                    for sweep in inputs:sweep['settings'].update(instrument=context,receive_profile=CONTROL_PROFILE,generator_output='unknown',generator_output_source='no_physical_readback')
                    for sweep in inputs:service.ingest_sweep(Sweep.from_dict(sweep))
                    monitoring.create_baseline(identifier,dict(name='Serial',location='Fixture',confirm=True))
                    prepared=monitoring.preflight(identifier)
                    def result(value):return dict(ready=value['ready'],checks=[dict(name=x['name'],ok=x['ok']) for x in value['checks']],hardware_validation=value['hardware_validation'])
                    cases.append(dict(changed_lna=changed,session=to_primitive(service.get_session(SESSION)),sweeps=inputs,undeclared=result(undeclared),prepared=result(prepared)))
            finally:monitoring.close();workers.close();service.close()
    return dict(fixture_version=1,synthetic=True,hardware_validation='not_run',cases=cases)


def emit_merge():
    import copy
    base=emit();counter=0
    def identity():
        nonlocal counter
        counter+=1
        return SimpleNamespace(hex=f'{counter:032x}')
    with tempfile.TemporaryDirectory(prefix='ori-python-guard-merge-') as directory:
        service=SessionService(store=SQLiteSessionStore(str(Path(directory)/'archive.sqlite')),clock=lambda:STAMP)
        workers=DeviceWorkerManager(service);monitoring=MonitoringService(service,workers)
        try:
            service.create_session({k:base['session'][k] for k in ('id','name','streams')})
            with patch('radioinspector.monitoring.models.uuid4',identity),patch('radioinspector.monitoring.models.utc_now',lambda:STAMP):
                p=monitoring.create(dict(name='Merge',site='Fixture',session_id=SESSION));identifier=p['id']
                monitoring.add_allowed(identifier,dict(name='Watched',start_hz=100060000,end_hz=100070000,watch_changes=True))
                for sweep in base['sweeps'][:20]:service.ingest_sweep(Sweep.from_dict(sweep))
                monitoring.create_baseline(identifier,dict(name='Quiet',location='Fixture',confirm=True))
                narrow=[-100.0]*9;narrow[4]=-92.0
                impulse=[-100.0]*9;impulse[4]=-70.0
                broad=[-80.0]*9
                watched=[-100.0]*9;watched[6]=-70.0;watched[7]=-70.0
                quiet=[-100.0]*9
                stages=[];index=20
                for pattern,count in [(narrow,3),(impulse,1),(broad,3),(watched,3),(quiet,8)]:
                    sweeps=[]
                    for _ in range(count):
                        sweep=copy.deepcopy(base['sweeps'][0]);sweep['id']=f'merge/{index}';sweep['levels']=pattern;sweep['settings']['sequence']=index
                        started=START+timedelta(seconds=4*index);sweep['interval']=dict(started_at=started.isoformat().replace('+00:00','Z'),ended_at=(started+timedelta(seconds=1)).isoformat().replace('+00:00','Z'))
                        service.ingest_sweep(Sweep.from_dict(sweep));sweeps.append(sweep);index+=1
                    report=monitoring.analyze(identifier)
                    stages.append(dict(sweeps=sweeps,report=report,events=monitoring.store.list('event',identifier,limit=1000),sources=monitoring.store.list('event-source',identifier,limit=1000)))
                return dict(fixture_version=1,synthetic=True,hardware_validation='not_run',stages=stages)
        finally:monitoring.close();workers.close();service.close()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    for name,source in [('guard-domain-v1.json',emit),('guard-operations-v1.json',emit_operations),('guard-serial-preflight-v1.json',emit_serial),('guard-merge-v1.json',emit_merge)]:
        output=ROOT/'contracts/fixtures'/name
        expected=(json.dumps(source(),ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')
        if args.check:
            if not output.is_file() or output.read_bytes()!=expected:parser.exit(1,f'Stale Python guard fixture: {output}\n')
        else:output.write_bytes(expected)
        print(output)
