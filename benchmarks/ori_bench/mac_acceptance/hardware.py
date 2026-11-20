"""Observe the dedicated live service; only the operator prepares/arms hardware."""
from __future__ import annotations
import ctypes
from dataclasses import replace
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
import uuid

from ..client import Client,segment
from ..config import Config,BenchmarkError
from ..endurance_node import publish_stop
from ..macos_rc import host
from ..runner import atomic_report,output_directory
from .checkpoints import assess,hold_ready
from .evidence import bundle_identity,private_json,read_json_lines,verify_host
from .load import Events,NativeWindow
from .node import PackageNode
from .proof import hardware_metrics
from .sources import SourceProof,export
from .freshness import SourceProgress


class Clock:
    def __init__(self):
        self.lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        class Base(ctypes.Structure):_fields_=[('numer',ctypes.c_uint32),('denom',ctypes.c_uint32)]
        base=Base()
        if self.lib.mach_timebase_info(ctypes.byref(base)) or not base.denom:raise RuntimeError('Mach clock unavailable')
        self.numer,self.denom=base.numer,base.denom
        self.lib.mach_continuous_time.restype=ctypes.c_uint64
        self.lib.mach_absolute_time.restype=ctypes.c_uint64
    def now(self):return self.lib.mach_continuous_time()*self.numer//self.denom
    def awake(self):return self.lib.mach_absolute_time()*self.numer//self.denom


def present(path):
    try:return stat.S_ISCHR(os.stat(path).st_mode)
    except (OSError,TypeError):return False


class Sampler:
    def __init__(self,client,project,ui_alive,ui_exit_code=lambda:None):
        self.client,self.project,self.ui_alive=client,project,ui_alive
        self.ui_exit_code=ui_exit_code
        self.path='/api/v1/monitoring-projects/'+segment(project)
        self.totals={};self.baselines={};self.sessions=set();self.identity=None
    def poll(self):
        c=self.client;status=c.json(self.path+'/status');p=status['project']
        sid=p['active_session_id'];self.sessions.update(p.get('session_ids',[]));self.sessions.add(sid)
        summary=c.json('/api/v1/sessions/'+segment(sid)+'/analytics/summary')
        self.totals[sid]=(summary['sweeps']['count'],summary['observations']['count'])
        latest=summary['sweeps'].get('latest') or {};receiver=summary['observations'].get('latest') or {}
        settings=latest.get('settings') or {};ident=settings.get('device_identity') or {}
        checks=status.get('receiver_checks',{}).get('items',[])
        evidence=next((row for row in checks if row.get('state')=='sampled' and row.get('receiver',{}).get('observation_id')),None)
        workers=status.get('workers',[])
        firmware=next((row.get('current',{}).get('receiver_firmware') for row in workers
                       if row.get('current',{}).get('receiver_firmware')),None)
        profiles=p.get('profile',{});analyzer=profiles.get('analyzer',{});rx=profiles.get('receiver',{})
        if ident.get('firmware') and firmware:
            self.identity=dict(mode='live',analyzer_model='tinySA Ultra' if any(word in str(ident.get('info','')).lower() for word in ('ultra','zs405')) else 'unconfirmed',
                receiver_model='AR-DV1' if rx.get('settings',{}).get('adapter')=='ardv1-serial' else 'unconfirmed',
                analyzer_firmware=ident['firmware'],receiver_firmware=firmware,analyzer_identity=ident,
                analyzer_profile=analyzer,receiver_profile=rx)
        preparation=p.get('preparation',{})
        row=dict(project_id=self.project,session_id=sid,active=status.get('active') is True,
            coverage=p.get('coverage'),runtime_instance_id=c.json('/api/v1/health').get('runtime_instance_id'),
            analyzer_sources=sum(value[0] for value in self.totals.values()),
            receiver_sources=sum(value[1] for value in self.totals.values()),
            analyzer_source_id=latest.get('id'),receiver_source_id=receiver.get('id'),
            analyzer_present=present(analyzer.get('port')),receiver_present=present(rx.get('port')),
            ui_alive=self.ui_alive(),ui_exit_code=self.ui_exit_code(),preparation_valid=preparation.get('receive_path_confirmed') is True
                and preparation.get('location_confirmed') is True and bool(preparation.get('antenna_notes'))
                and all(w.get('alive') for w in workers) and bool(workers),
            worker_errors=[str(w.get('last_error')) for w in workers if w.get('last_error')],
            guard_error=status.get('error'),bands=[band for w in workers for band in w.get('current',{}).get('scan_coverage',{}).get('bands',[])])
        bids=list((p.get('band_baselines') or {}).values()) if p.get('scan_plan') else [p.get('baseline_id')]
        records=[]
        for bid in bids:
            if not bid:continue
            if bid not in self.baselines:self.baselines[bid]=c.json(self.path+'/baselines/'+segment(bid))
            records.append(self.baselines[bid])
        if records:
            def span(b):return (datetime.fromisoformat(b['ended_at'].replace('Z','+00:00'))-datetime.fromisoformat(b['started_at'].replace('Z','+00:00'))).total_seconds()
            row.update(baseline_id=';'.join(str(b['id']) for b in records),
                baseline_confirmed=len(records)==len(bids) and all(b.get('confirmed') is True for b in records),
                baseline_sources=min(len(b.get('source_refs',[])) for b in records),baseline_span=min(span(b) for b in records))
            periods=[b.get('sweep_period_seconds') for b in records]
            if all(type(value) in (int,float) and value>0 for value in periods):
                row['analyzer_max_silence_seconds']=max(10,3*max(periods))
            bands=(p.get('scan_plan') or {}).get('config',{}).get('bands',[])
            if bands:row['analyzer_max_silence_seconds']=max(b['max_revisit_seconds'] for b in bands)
        period=rx.get('settings',{}).get('period_s',.5)
        dwell=p.get('parameters',{}).get('dwell_seconds',10)
        if type(period) in (int,float) and period>0 and type(dwell) in (int,float) and dwell>0:
            row['receiver_max_silence_seconds']=max(10,3*period+dwell)
        if evidence:row['receiver_evidence_id']=evidence['id']
        events=c.json(self.path+'/events?limit=1').get('items',[])
        if events:
            row['event_id']=events[0]['id'];row['event_source_ids']=[ref['sweep_id'] for ref in events[0].get('source_refs',[]) if ref.get('sweep_id')]
        return row


class Collector:
    def __init__(self,sampler,path,run_id,sha,clock,receipts):
        self.sampler,self.clock,self.receipts=sampler,clock,receipts
        self.events=Events(path,run_id);self.sha=sha;self.stop=threading.Event();self.ready=threading.Event();self.lock=threading.RLock()
        self.rows=[];self.errors=[];self.allowed=None;self.previous=None;self.identity_written=False;self.armed=False;self.progress=SourceProgress();self.gaps=[]
        self.hold_notified=False
        self.thread=threading.Thread(target=self.run,name='mac-hardware-evidence')
    def emit(self,stage,fields):
        self.events.emit(stage,dict(fields,component_sha256=self.sha,at_ns=self.clock.now()))
    def error(self,message):
        if message in self.errors:return
        self.errors.append(message);self.emit('hardware.error',dict(message=message))
        self.ready.set()
        if not (self.receipts/'wire-freeze.request').exists():publish_stop(self.receipts/'wire-freeze.request',self.events.run_id)
        print('\nОШИБКА: '+message+'. Обмен сохранён; автоматического перезапуска нет.',flush=True)
    def start(self):self.thread.start()
    def run(self):
        while not self.stop.is_set():
            try:
                continuous,awake=self.clock.now(),self.clock.awake()
                row=self.sampler.poll();row['at_ns']=continuous;row['id']='sample-'+str(len(self.rows))
                if self.previous:
                    row['sleep_delta_ns']=max(0,(continuous-self.previous[0])-(awake-self.previous[1]))
                self.previous=(continuous,awake)
                with self.lock:
                    self.rows.append(row);allowed=self.allowed
                    self.emit('hardware.sample',row)
                    self.ready.set()
                    if allowed and not self.hold_notified and hold_ready(allowed[0],self.rows[self.checkpoint_start:]):
                        self.hold_notified=True
                        print('\nНаблюдаемый минимальный интервал подтверждён. Теперь можно вернуть UI/подключить прибор и завершить шаг.',flush=True)
                    if self.sampler.identity and not self.identity_written:
                        self.emit('hardware.identity',self.sampler.identity);self.identity_written=True
                    planned=allowed and continuous<=allowed[2] and allowed[0] in ('usb_analyzer','usb_receiver','sleep')
                    intervals=self.gaps+([(allowed[1],allowed[2],allowed[0])] if planned else [])
                    if self.armed:
                        for error in self.progress.observe(row,planned=intervals):self.error(error)
                    if not planned:
                        if self.armed and (row.get('guard_error') or row.get('worker_errors')):self.error(str(row.get('guard_error') or row['worker_errors']))
                        if any(b.get('deadline_missed') is True for b in row['bands']):self.error('Scan revisit deadline missed')
                        if getattr(self,'armed',False) and not row['active']:self.error('Guard stopped outside a declared checkpoint')
            except Exception as error:
                with self.lock:
                    allowed=self.allowed
                    if not allowed or allowed[0]!='sleep' or self.clock.now()>allowed[2]:self.error(str(error))
            self.stop.wait(2)
    def checkpoint(self,name,prompt,*,limit=300,reopen=None):
        if not self.ready.wait(60):raise BenchmarkError('no_samples','First live observation did not complete within60seconds')
        with self.lock:
            if self.errors:raise BenchmarkError('collector_failed',self.errors[0])
            start=len(self.rows)-1
            if start<0:raise BenchmarkError('no_samples','No live observation available')
            began=self.clock.now();self.allowed=(name,began,began+limit*1_000_000_000)
            self.checkpoint_start=start;self.hold_notified=False
        try:
            input('\n'+prompt+'\nПосле выполнения нажмите Enter в этом Терминале: ')
            if reopen:reopen()
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                with self.lock:
                    if self.clock.now()>self.allowed[2]:raise BenchmarkError('checkpoint_timeout',name+' exceeded its declared time window')
                    rows=self.rows[max(0,start):];ok,reason=assess(name,rows)
                    if ok:
                        self.emit('checkpoint.verified',dict(name=name,observed=True,evidence_ids=[r['id'] for r in rows]))
                        if name in ('usb_analyzer','usb_receiver','sleep'):
                            ended=self.clock.now();self.gaps.append((began,ended,name))
                            self.emit('hardware.planned_gap',dict(kind=name,start_ns=began,end_ns=ended))
                        return
                time.sleep(2)
            raise BenchmarkError('checkpoint_failed',name+': '+reason)
        finally:
            with self.lock:self.allowed=None
    def close(self):
        self.stop.set()
        if self.thread.ident:self.thread.join(35)
        if self.thread.is_alive():raise BenchmarkError('collector_busy','Hardware observer did not join')
        self.events.close()


def require_load(path,identity):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>16*1024**2:raise BenchmarkError('load_required','Load report missing/invalid')
    from tools.modernization.api_parity import load_json
    import math
    try:report=load_json(path.read_bytes(),16*1024**2)
    except ValueError as error:raise BenchmarkError('load_required','Strict complete load report required') from error
    if not isinstance(report,dict):raise BenchmarkError('load_required','Load report must be an object')
    def number(value,minimum,maximum=float('inf')):
        return type(value) in (int,float) and math.isfinite(value) and minimum<=value<=maximum
    native=report.get('native',{});counts=report.get('counts',{})
    complete_counts=all(all(type(counts.get(kind,{}).get(key)) is int and counts[kind][key]==18000
        for key in ('offered','acknowledged','verified')) and all(counts[kind].get(key,0)==0
        for key in ('rejected','queue_dropped','transport_unknown','injected_gap','cancelled','invalid_ack'))
        for kind in ('sweep','observation'))
    metric_ok=True
    for name,limit,minimum in [('commit_to_presented',250,18000),('reaction',100,100),('queue_ack',200,100)]:
        metric=native.get(name,{})
        metric_ok &= metric.get('status')=='passed' and metric.get('missing')==[] \
            and type(metric.get('samples')) is int and metric['samples']>=minimum and number(metric.get('p95_ms'),0,limit)
    pan=native.get('pan_zoom',{})
    components=report.get('components',{})
    complete=report.get('profile')=='macos-final-load' and report.get('successful') is True \
        and report.get('kit_verified_after_run') is True and isinstance(report.get('kit_manifest_sha256'),str) \
        and len(report['kit_manifest_sha256'])==64 and set(report['kit_manifest_sha256'])<=set('0123456789abcdef') \
        and report.get('candidate_verified_after_run') is True and type(report.get('producer_status')) is int and report['producer_status']==0 \
        and number(report.get('production_elapsed_seconds'),1800) and complete_counts \
        and report.get('errors')==[] and report.get('queue_errors')==[] \
        and all(components.get(key,{}).get('status')=='passed' for key in
            ('production','native','resources','online_backup','final_backup','restore','cleanup')) \
        and native.get('status')=='passed' and native.get('errors')==[] and metric_ok \
        and native.get('diagnostics',{}).get('required') is True \
        and type(pan.get('windows')) is int and pan['windows']>=3 and number(pan.get('minimum_fps'),30) \
        and report.get('resources',{}).get('status')=='passed' \
        and report.get('restore',{}).get('status')=='passed' \
        and report['restore'].get('source_counts')=={'sweep':18000,'observation':18000} \
        and report.get('software_concurrency_observed') is True \
        and isinstance(report.get('run_id'),str) and 0<len(report['run_id'])<=64
    if report.get('point2_passed') is not True or report.get('smoke_only') is not False \
            or not complete \
            or any(report.get('identity',{}).get(k)!=identity[k] for k in ('source_revision','core_sha256','native_sha256','supervisor_sha256')):
        raise BenchmarkError('load_required','First pass the full load on this exact core/UI build')
    return report['run_id']


def require_load_pair(first,second,identity,*,expected_kit_sha256=None):
    from tools.modernization.api_parity import load_json
    runs=[require_load(path,identity) for path in (first,second)]
    reports=[load_json(Path(path).read_bytes(),16*1024**2) for path in (first,second)]
    transports=[r.get('actual_transports') for r in reports]
    kits={r.get('kit_manifest_sha256') for r in reports}
    if runs[0]==runs[1] or transports not in ([['binary-v1'],['sse']],[['sse'],['binary-v1']]):
        raise BenchmarkError('load_pair_required','Two distinct full runs must actually observe binary-v1 and SSE')
    if len(kits)!=1 or (expected_kit_sha256 is not None and kits!={expected_kit_sha256}):
        raise BenchmarkError('load_pair_required','Both loads and hardware must use the same immutable engineering kit')
    return runs


def finalize_hardware_evidence(report,root):
    """Evaluate closed observers even when backup/restore failed independently."""
    run_id=report.get('run_id');identity=report.get('identity',{})
    if not run_id or not identity:return
    path=Path(root)/'hardware.jsonl'
    if path.exists():
        report['hardware']=hardware_metrics(read_json_lines(path),run_id=run_id,
            core_sha256=identity['core_sha256'],elapsed_seconds=report.get('elapsed_seconds',0))
    native=dict(status='not_run',files=0,received_sources=0,errors=[],
                presentation='Full native performance is measured by the two load runs, not eight-hour UI uptime')
    sources=set()
    for directory in Path(root).glob('native-*'):
        if not directory.is_dir():continue
        for trace in directory.glob('native-*.jsonl'):
            native['files']+=1;found_identity=False
            for row in read_json_lines(trace):
                if row.get('run_id')!=run_id:native['errors'].append('native_run_mismatch');continue
                if row.get('stage')=='native_identity':
                    found_identity=row.get('component_sha256')==identity.get('native_sha256')
                elif row.get('stage')=='native.error':native['errors'].append(str(row.get('message','native_error')))
                elif row.get('stage')=='received':sources.add(row.get('source_id'))
            if not found_identity:native['errors'].append('native_identity_missing')
        receipts=list(directory.glob('native-finished-*.json'))
        if not receipts:native['errors'].append('native_close_receipt_missing')
        for receipt in receipts:
            value=json.loads(receipt.read_bytes())
            if value.get('run_id')!=run_id or value.get('successful') is not True:native['errors'].append('native_trace_incomplete')
    native['received_sources']=len(sources)
    if native['files']:native['status']='failed' if native['errors'] else 'passed'
    report['native']=native
    if native['errors']:report['errors'].append(dict(code='native_evidence_failed',message='; '.join(sorted(set(native['errors'])))))


def run_hardware(app,manifest,output,load_report,companion_load_report=None,*,expected_kit_sha256=None):
    root=output_directory(output).resolve(strict=True);report=dict(schema_version=1,profile='macos-eight-hour-live',
        successful=False,point4_passed=False,hardware_tested=False,errors=[],host=host())
    windows=[];collector=None;pointer=None;data=None;pointer_owned=False;reopen_window=None;ui_running=lambda:False
    clock=start=None
    try:
        identity=bundle_identity(app,manifest);report['identity']=identity
        if not verify_host(report['host']):raise BenchmarkError('wrong_host','Target is Apple Silicon/macOS27.3/16+GiB')
        if companion_load_report is None:
            raise BenchmarkError('load_pair_required','Both full binary-v1 and SSE reports are required before opening hardware')
        if expected_kit_sha256 is None:raise BenchmarkError('immutable_kit_required','Use the supplied immutable kit for physical acceptance')
        report['load_run_ids']=require_load_pair(load_report,companion_load_report,identity,expected_kit_sha256=expected_kit_sha256)
        report['load_run_id']=report['load_run_ids'][0]
        subprocess.run(['codesign','--verify','--strict','--deep',str(app)],check=True,timeout=60)
        run_id='machw-'+uuid.uuid4().hex;report['run_id']=run_id;clock=Clock()
        data=root/'data';data.mkdir(mode=0o700);receipts=root/'core';receipts.mkdir(mode=0o700)
        profile=receipts/'profile.json'
        private_json(profile,dict(version=1,run_id=run_id,phase='hardware',data_dir=str(data),output_dir=str(receipts),expected_core_sha256=identity['core_sha256']))
        base=Path.home()/'Library/Application Support/OpenRadioInspector/macOS-Acceptance'
        base.mkdir(mode=0o700,parents=True,exist_ok=True)
        if base.is_symlink() or base.stat().st_uid!=os.geteuid() or base.stat().st_mode&0o077:
            raise BenchmarkError('private_scope','Acceptance service scope must be private and owned')
        pointer=base/'active.json'
        private_json(pointer,dict(version=1,run_id=run_id,profile=str(profile)))
        pointer_owned=True
        def reopen():
            if len(windows)>=16:raise BenchmarkError('native_restart_budget','Native restart budget exhausted; preserve diagnostics')
            native=root/('native-'+str(len(windows)));native.mkdir(mode=0o700)
            p=native/'profile.json'
            private_json(p,dict(version=1,run_id=run_id,phase='hardware',data_dir=str(data),output_dir=str(native),
                expected_ui_sha256=identity['native_sha256'],core_url='http://127.0.0.1:8774',session_id=''))
            window=NativeWindow(app,native,p);windows.append(window);window.start()
        def alive():
            return any(w.owner and (w.owner.status() or {}).get('phase')=='running' for w in windows)
        def ui_exit_code():
            if alive():return None
            if not windows or windows[-1].owner is None:return -1
            window=windows[-1];state=window.owner.status() or {}
            receipts=list(window.root.glob('native-finished-*.json'))
            return 0 if state.get('phase')=='exited' and state.get('exit_code')==0 and receipts \
                and all(json.loads(p.read_bytes()).get('successful') is True for p in receipts) else -1
        reopen_window=reopen;ui_running=alive
        reopen()
        print('Открыт отдельный тестовый архив. Запустите службу в приложении, подготовьте настоящий сеанс tinySA Ultra + AR-DV1 и создайте проект. Охрану пока не включайте.',flush=True)
        client=Client(Config('http://127.0.0.1:8774',root/'unused',request_timeout=10))
        input('После подготовки нажмите Enter: ')
        projects=client.json('/api/v1/monitoring-projects?limit=100')['items']
        for index,p in enumerate(projects,1):print(str(index)+'. '+p['name']+' ['+p['id']+']')
        selected=int(input('Номер тестового проекта: '))-1
        if not 0<=selected<len(projects):raise BenchmarkError('invalid_project','Select an existing test project')
        project=projects[selected]['id'];path='/api/v1/monitoring-projects/'+segment(project)
        client.json(path+'/settings',method='POST',payload={'autostart':False,'archive':{
            'minimum_retention_seconds':43200,'reserve_backup':True}})
        sampler=Sampler(client,project,alive,ui_exit_code);collector=Collector(sampler,root/'hardware.jsonl',run_id,identity['core_sha256'],clock,receipts);collector.start()
        collector.checkpoint('baseline','Соберите не менее 60 секунд и 20 полных свипов для каждого диапазона; подтвердите эталон оператором.',limit=1800)
        storage=client.json(path+'/storage');report['archive_budget']=storage
        if storage.get('ready') is not True or storage.get('backup_reserve_requested') is not True \
                or not isinstance(storage.get('bytes_per_second'),(int,float)) or storage.get('estimated_retention_seconds',0)<43200:
            raise BenchmarkError('insufficient_space','Measured 8h +50% budget and backup reserve not available; do not arm')
        collector.checkpoint('guard','Включите охрану вручную; дождитесь новых панорам.')
        collector.armed=True;start=clock.now();report['hardware_tested']=True
        collector.emit('hardware.window.begin',{})
        collector.checkpoint('receiver','Дождитесь анализа AR-DV1 и новых наблюдений при продолжающемся обзоре tinySA.')
        collector.checkpoint('event','Получите новое отклонение обычного сигнала (без генератора tinySA); откройте событие и его исходник.',limit=1800)
        collector.checkpoint('source_search','Включите «Поиск источника», дождитесь узкого обзора, затем вернитесь к широкому.')
        collector.checkpoint('ui_quit','Нажмите Command-Q. Дождитесь сообщения о 30 наблюдаемых секундах закрытого UI; Enter вновь откроет интерфейс.',reopen=reopen)
        collector.checkpoint('usb_analyzer','Отключите USB tinySA. После сообщения о 10 наблюдаемых секундах отсутствия подключите вновь. Повторите подготовку и верните охрану; AR-DV1 остаётся подключён.')
        collector.checkpoint('usb_receiver','Отключите USB AR-DV1. После сообщения о 10 наблюдаемых секундах отсутствия подключите вновь. Повторите подготовку/возврат в охрану; tinySA продолжает обзор.')
        collector.checkpoint('sleep','Выполните ручной сон Mac минимум на 10 секунд. После пробуждения явно запустите службу, перепроверьте приборы и включите охрану.',limit=900)
        while clock.now()-start<28_800_000_000_000:
            if collector.errors:raise BenchmarkError('hardware_failed','A spontaneous failure was recorded; preserve diagnostics')
            remaining=(28_800_000_000_000-(clock.now()-start))/1e9
            print('Осталось реального времени: '+str(round(remaining/3600,2))+' ч. Охрану оставьте включённой.',flush=True)
            collector.stop.wait(min(60,remaining))
        report['elapsed_seconds']=(clock.now()-start)/1e9
        collector.emit('hardware.window.end',{})
        collector.armed=False
        input('Восемь часов завершены. Остановите охрану в приложении и нажмите Enter: ')
        if client.json(path+'/status')['active'] is True:raise BenchmarkError('guard_busy','Stop only the test guard before final source verification')
        collector.close();collector=None
        # Quiescent original export and owner backup cover the same committed set.
        proof=SourceProof(root/'originals.sqlite')
        try:
            sessions=client.json('/api/v1/sessions?limit=1000')
            if len(sessions)>=1000:raise BenchmarkError('source_limit','Unexpected session count in isolated test archive')
            for session in sessions:export(client,session['id'],proof.original)
            publish_stop(receipts/'backup.request',run_id)
            from .budget import wait_for_backup
            report['backup']=wait_for_backup(receipts)
            if report['backup']['phase']!='completed':raise BenchmarkError('backup_failed','Owner backup failed')
            restore=root/'restore';restore.mkdir(mode=0o700)
            node=PackageNode(Path(app)/'Contents/Helpers/ori-core',restore,run_id+'-restore',restore_bundle=receipts/'backup')
            try:
                restored=Client(Config(node.start(),root/'unused-restore'))
                for session in sessions:export(restored,session['id'],proof.restored)
            finally:node.close(expect_success=False)
            for trace in receipts.glob('core-*.jsonl'):
                for row in read_json_lines(trace):
                    if row.get('run_id')!=run_id or row.get('component_sha256')!=identity['core_sha256']:
                        raise BenchmarkError('source_commit_invalid','Commit receipt build/run differs')
                    if row.get('stage')=='committed':proof.committed(row)
            source_proof=proof.finish(require_commits=True);report['source_proof']=source_proof
        finally:proof.close()
        # Append final immutable receipts after the continuous observer is flushed.
        with (root/'hardware.jsonl').open('ab') as stream:
            rows=list(read_json_lines(root/'hardware.jsonl'));last=dict(next(r for r in reversed(rows) if r['stage']=='hardware.sample'))
            last.update(stage='checkpoint.sample',id='restore-proof',at_ns=clock.now(),restore_proof=source_proof)
            final=[last,dict(v=1,stage='hardware.source_proof',run_id=run_id,component_sha256=identity['core_sha256'],at_ns=clock.now(),**source_proof),
                dict(v=1,stage='checkpoint.verified',run_id=run_id,component_sha256=identity['core_sha256'],at_ns=clock.now(),name='history_restore',observed=True,evidence_ids=['restore-proof'])]
            for row in final:stream.write(json.dumps(row,allow_nan=False).encode()+b'\n')
            stream.flush();os.fsync(stream.fileno())
        report['hardware']=hardware_metrics(read_json_lines(root/'hardware.jsonl'),run_id=run_id,core_sha256=identity['core_sha256'],elapsed_seconds=report['elapsed_seconds'])
        report['successful']=report['hardware']['status']=='passed';report['point4_passed']=report['successful']
    except (Exception,KeyboardInterrupt) as error:
        report['errors'].append(dict(code=getattr(error,'code','hardware_failed'),message=str(error)))
    finally:
        if collector:
            if start is not None and clock is not None and 'elapsed_seconds' not in report:
                report['elapsed_seconds']=(clock.now()-start)/1e9
                collector.emit('hardware.window.end',dict(aborted=True))
            try:collector.close()
            except Exception as error:report['errors'].append(dict(code='collector_cleanup',message=str(error)))
        # The native helper confirms private core stop AND SMAppService removal.
        # A shutdown-only ACK leaves an enabled, unloaded LaunchAgent behind.
        if data and pointer_owned and pointer.exists():
            try:
                if not ui_running():
                    if reopen_window is None:raise BenchmarkError('service_stop_required','No verified native cleanup helper')
                    reopen_window()
                report['service_cleanup']=windows[-1].finish_service(report['run_id'])
                if json.loads(pointer.read_bytes()).get('run_id')!=report['run_id']:
                    raise BenchmarkError('service_stop_required','Active run pointer changed; retained')
                pointer.unlink()
            except Exception as error:
                report['errors'].append(dict(code='service_stop_required',message=str(error)+'; active pointer retained'))
                try:
                    result=subprocess.run([str(Path(app)/'Contents/Helpers/ori-supervisor'),'shutdown',str(data)],
                                          stdin=subprocess.DEVNULL,capture_output=True,timeout=150)
                    (root/'service-shutdown.log').write_bytes(result.stdout+result.stderr)
                except Exception as stop_error:report['errors'].append(dict(code='service_shutdown_failed',message=str(stop_error)))
        for window in windows:
            try:window.stop(report.get('run_id','unknown'),required=report.get('point4_passed') is True)
            except Exception as error:report['errors'].append(dict(code='ui_cleanup',message=str(error)))
        if report.get('identity'):
            try:
                if bundle_identity(app,manifest)!=report['identity']:raise BenchmarkError('candidate_changed','App changed during hardware run')
                report['candidate_verified_after_run']=True
            except Exception as error:report['errors'].append(dict(code=getattr(error,'code','candidate_changed'),message=str(error)))
        try:finalize_hardware_evidence(report,root)
        except Exception as error:report['errors'].append(dict(code='hardware_evidence_failed',message=str(error)))
        if report.get('point4_passed'):
            try:
                traces=list((root/'core').glob('core-*.jsonl'))
                if not traces:raise BenchmarkError('core_evidence_missing','No original core trace')
                for trace in traces:
                    rows=list(read_json_lines(trace))
                    if any(row.get('run_id')!=report['run_id'] or row.get('component_sha256')!=report['identity']['core_sha256'] for row in rows) \
                            or not any(row.get('stage')=='core.finished' and row.get('successful') is True for row in rows):
                        raise BenchmarkError('core_evidence_incomplete','Original core did not complete its evidence trace')
            except Exception as error:report['errors'].append(dict(code=getattr(error,'code','core_evidence_invalid'),message=str(error)))
        if report['errors']:report.update(successful=False,point4_passed=False)
        report['finished_at']=datetime.now(timezone.utc).isoformat();atomic_report(root,report)
    return report,0 if report['successful'] else 1
