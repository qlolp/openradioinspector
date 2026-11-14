from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
from urllib.parse import urlsplit

from . import VERSION
from .client import Client,HTTPFailure,segment
from .config import BenchmarkError
from .ledger import Ledger
from .metrics import Histogram,Sampler,host
from .source import audio_segment,encode,identifier,measurement_hash,observation,scheduled,session,sweep
from . import verify

LATENCIES=('offered_to_ack_ms','request_ms','schedule_lag_ms','history_summary_ms',
           'history_full_payload_ms','offered_to_render_ms')
TARGET={'node':'Raspberry Pi 5 / 8 GiB / SSD','clients':3,'reference_duration_seconds':1800,
    'reference_sweep_rate':10,'reference_observation_rate':10,'reference_points':4096,
    'reference_audio_sample_rate':48000,'reference_history_sweeps':1000000,'reference_history_points':290,
    'history_first100_p95_ms':500,'committed_to_render_p95_ms':250,'rust_core_rss_bytes':256*1024**2,
    'research_worker_rss_bytes':768*1024**2,'background_processes_rss_bytes':1536*1024**2}


def output_directory(path):
    path=Path(path).expanduser().absolute()
    resolved=path.resolve()
    def git_marker(parent):
        marker=parent/'.git'
        return marker.is_file() or (marker.is_dir() and (marker/'HEAD').is_file())
    if any(git_marker(parent) for parent in (resolved,*resolved.parents)):
        raise BenchmarkError('unsafe_output','Runtime artifacts must be outside Git worktrees')
    if path.exists() or path.is_symlink():
        raise BenchmarkError('unsafe_output','Output directory must be new; nothing was overwritten')
    path.mkdir(mode=0o700,parents=True)
    (path/'owner.json').write_bytes(encode({'owner':'ori-benchmarks','version':1})+b'\n')
    return path


def atomic_report(output,report):
    temporary=output/'report.json.tmp'
    try:
        with temporary.open('xb') as target:
            target.write(encode(report)+b'\n');target.flush();os.fsync(target.fileno())
        os.replace(temporary,output/'report.json')
    finally:temporary.unlink(missing_ok=True)


def preflight(client,config):
    capabilities=client.json('/api/v1/capabilities')
    features=capabilities.get('features',{})
    required=['measurement_batch_ingest','session_archive_export_ndjson']
    if config.planned_counts()['audio']:required.append('audio_segment_ingest')
    if config.observe_render:required.append('monitoring_projects')
    if capabilities.get('persistent_archive') is not True or any(features.get(key) is not True for key in required):
        raise BenchmarkError('unsupported','Core lacks required persistent ingest/export/audio capability')
    guard={'status':'unmeasured','reason':'Guard capability is not advertised'}
    if features.get('monitoring_guard') is True:
        after='';checked=0;pages=0
        while True:
            pages+=1
            if pages>11:raise BenchmarkError('unsafe_target','Guard preflight exceeded the bounded page count')
            result=client.json('/api/v1/monitoring-projects?limit=100'+('&after='+segment(after) if after else ''))
            items=result.get('items')
            if not isinstance(items,list) or len(items)>100 or checked+len(items)>1000:
                raise BenchmarkError('unsafe_target','Cannot establish bounded guard inactivity')
            for project in items:
                project_id=project.get('id')
                if not isinstance(project_id,str) or not 0<len(project_id)<=1024:
                    raise BenchmarkError('unsafe_target','Invalid project identity in guard preflight')
                state=client.json('/api/v1/monitoring-projects/'+segment(project_id)+'/status')
                if state.get('active') is not False or state.get('receiver_busy') is not False:
                    raise BenchmarkError('unsafe_target','An active guard or receiver blocks synthetic load')
                checked+=1
            cursor=result.get('next_cursor')
            if cursor is None:break
            if not isinstance(cursor,str) or not 0<len(cursor)<=1024 or cursor==after:
                raise BenchmarkError('unsafe_target','Guard preflight cursor did not advance')
            after=cursor
        guard={'status':'passed','checked_projects':checked,'scope':'preflight point-in-time; no guard mutation'}
    path=client.session_path(config)
    try:client.json(path)
    except HTTPFailure as error:
        if error.status!=404:raise
    else:raise BenchmarkError('existing_session','Refusing to write to an existing session')
    if config.observe_render:
        from .browser import resolve_node
        resolve_node(config)
    health=client.json('/api/v1/health')
    created=client.json('/api/v1/sessions',method='POST',payload=session(config),status=201)
    if created.get('id')!=config.session_id or {r['id'] for r in created.get('streams',[])}!={config.spectrum_id,config.receiver_id}:
        raise BenchmarkError('invalid_session','Creation acknowledgement does not establish the owned synthetic session')
    return capabilities,health,guard


def send(client,config,ledger,kind,index,histograms):
    key=identifier(config,kind,index)
    row=ledger.get(key)
    if config.scenario=='fault' and index%config.fault_every==0 and kind=='sweep' and config.fault_kind=='client-gap':
        ledger.status(key,'injected_gap');return
    value=(sweep if kind=='sweep' else observation if kind=='observation' else audio_segment)(config,index)
    path=client.session_path(config)
    if kind=='audio':path+='/audio-segments';payload=value
    else:
        path+=('/sweeps' if kind=='sweep' else '/observations')+'/batch'
        payload={'items':[value]}
        if config.scenario=='fault' and config.fault_kind=='invalid-batch' and kind=='sweep' and index%config.fault_every==0:
            value=dict(value,frequencies_hz=[],levels=[]);payload={'items':[value]}
    started=time.monotonic_ns()
    try:
        result=client.json(path,method='POST',payload=payload,status=201)
        if kind=='audio':
            valid=result.get('id')==key and result.get('sha256')==value['sha256']
        else:
            items=result.get('items',[])
            valid=result.get('count')==1 and len(items)==1 and items[0].get('id')==key \
                and measurement_hash(kind,items[0])==measurement_hash(kind,value)
        if not valid:raise BenchmarkError('invalid_ack','Invalid source acknowledgement')
        ledger.status(key,'accepted',201)
        histograms['offered_to_ack_ms'].add((time.monotonic_ns()-row['offered_ns'])/1e6)
    except HTTPFailure as error:ledger.status(key,'rejected',error.status)
    except BenchmarkError as error:
        ledger.status(key,'transport_unknown' if error.code=='transport_error' else 'invalid_ack')
    except (ValueError,TypeError,KeyError,OverflowError):ledger.status(key,'invalid_ack')
    finally:histograms['request_ms'].add((time.monotonic_ns()-started)/1e6)


def run_load(config,ledger,histograms):
    pending=queue.Queue(maxsize=config.queue_capacity)
    finished=threading.Event();cancelled=threading.Event()
    def worker():
        client=Client(config)
        while True:
            try:kind,index=pending.get(timeout=.05)
            except queue.Empty:
                if finished.is_set():return
                continue
            try:
                if cancelled.is_set():ledger.status(identifier(config,kind,index),'cancelled')
                else:send(client,config,ledger,kind,index,histograms)
            except Exception:ledger.status(identifier(config,kind,index),'invalid_ack')
            finally:pending.task_done()
    workers=[threading.Thread(target=worker,name=f'benchmark-producer-{i}',daemon=True) for i in range(config.workers)]
    for thread in workers:thread.start()
    anchor=time.monotonic_ns()
    try:
        for due_us,kind,index in scheduled(config):
            due=anchor+int(due_us*1000/config.speed)
            while True:
                delay=(due-time.monotonic_ns())/1e9
                if delay<=0:break
                time.sleep(min(delay,.05))
            now=ledger.offer(config,kind,index,due)
            histograms['schedule_lag_ms'].add(max(0,(now-due)/1e6))
            try:pending.put_nowait((kind,index))
            except queue.Full:ledger.status(identifier(config,kind,index),'queue_full')
        if config.workload_profile == 'macos-rc1':
            # Without a WAV closing exactly at the horizon, the final two
            # periodic measurements are offered one period before it. Observe
            # the entire requested interval, retaining the original drain bound.
            horizon = anchor + int(config.duration_us * 1000 / config.speed)
            while (remaining := (horizon - time.monotonic_ns()) / 1e9) > 0:
                time.sleep(min(remaining, .05))
    finally:
        finished.set()
        deadline=time.monotonic()+config.drain_seconds
        while pending.unfinished_tasks and time.monotonic()<deadline:time.sleep(.01)
        if pending.unfinished_tasks:cancelled.set()
        for thread in workers:thread.join(config.request_timeout+2)
        if any(thread.is_alive() for thread in workers):
            raise BenchmarkError('producers_incomplete','Producer threads did not finish within the drain deadline')
    return {'scheduled_wall_seconds':config.duration_seconds/config.speed,
            'production_elapsed_seconds':(time.monotonic_ns()-anchor)/1e9}


def run_history(config,client,capabilities,ledger,histograms):
    features=capabilities['features']
    server_limit=features.get('measurement_batch_limit',100)
    byte_limit=features.get('measurement_batch_max_request_bytes',1048576)
    if type(server_limit) is not int or type(byte_limit) is not int or not 1<=server_limit<=100 or not 1<=byte_limit<=16*1024**2:
        raise BenchmarkError('invalid_capabilities','Invalid bounded batch capability')
    maximum=min(config.batch_size,server_limit)
    items=[];indexes=[];size=12
    def flush():
        nonlocal items,indexes,size
        if not items:return
        offered=ledger.offer_many(config,'sweep',indexes)
        started=time.monotonic_ns()
        try:
            result=client.json(client.session_path(config)+'/sweeps/batch',method='POST',payload={'items':items},status=201)
            returned=result.get('items',[])
            if result.get('count')!=len(items) or len(returned)!=len(items) or any(
                measurement_hash('sweep',source)!=measurement_hash('sweep',actual) for source,actual in zip(items,returned)):
                raise BenchmarkError('invalid_ack','Invalid batch acknowledgement')
            for index in indexes:ledger.status(identifier(config,'sweep',index),'accepted',201)
            histograms['offered_to_ack_ms'].add((time.monotonic_ns()-offered)/1e6)
        except HTTPFailure as error:
            for index in indexes:ledger.status(identifier(config,'sweep',index),'rejected',error.status)
        except BenchmarkError as error:
            for index in indexes:ledger.status(identifier(config,'sweep',index),
                'transport_unknown' if error.code=='transport_error' else 'invalid_ack')
        finally:histograms['request_ms'].add((time.monotonic_ns()-started)/1e6)
        items=[];indexes=[];size=12
    started=time.monotonic()
    for index in range(config.sweeps):
        value=sweep(config,index);needed=len(encode(value))+1
        if items and (len(items)>=maximum or size+needed>byte_limit):flush()
        if needed+12>byte_limit:
            ledger.offer_many(config,'sweep',[index]);ledger.status(value['id'],'rejected');continue
        items.append(value);indexes.append(index);size+=needed
    flush()
    return {'preload_elapsed_seconds':time.monotonic()-started,'maximum_batch_items':maximum,
            'maximum_batch_request_bytes':byte_limit,'offered_to_ack_samples_are_batches':True}


def run(config):
    output=output_directory(config.output_dir)
    ledger=Ledger(output/'offers.sqlite')
    histogram={name:Histogram() for name in LATENCIES}
    client=Client(config);sampler=Sampler(config);started=time.monotonic()
    report={'report_version':1,'harness_version':VERSION,'run_status':'failed','host':host(),
        'endpoint':{'origin':config.base_url.rstrip('/'),'label':config.endpoint_label},
        'workload':config.public(),'reference_targets':TARGET,
        'checks':{key:{'status':'unmeasured'} for key in ('archive_integrity','complete_delivery',
            'offered_accounting','history_summary_p95','render_latency','pi_acceptance','physical_acceptance',
            'seven_day_service_endurance','background_concurrency')},'errors':[]}
    functional=False;status=1;observer=None
    try:
        with sampler:
            capabilities,health,guard=preflight(client,config)
            report['guard_preflight']=guard
            report['event_watermark_before']=health.get('last_event_id')
            if config.observe_render:
                from .browser import OwnedObserver
                observer=OwnedObserver(config,client,output)
                report['observer_setup']=observer.start()
            try:
                report['production']=(run_history(config,client,capabilities,ledger,histogram) if config.mode=='history'
                                      else run_load(config,ledger,histogram))
            finally:
                if observer is not None:
                    report['observer_completed']=observer.close()
            report['history']=verify.history(client,config,capabilities,histogram,ledger)
            report['checks']['archive_integrity']=verify.archive(client,config,ledger)
            if config.planned_counts()['audio']:report['audio']=verify.audio(client,config,ledger,output)
            else:report['audio']=ledger.audio_counts()
            missing=ledger.db.execute("SELECT COUNT(*) FROM offers WHERE status='accepted' AND verified=0").fetchone()[0]
            if missing:raise BenchmarkError('archive_invalid','Acknowledged source input is missing from the archive')
            newest=[row[0] for row in ledger.db.execute("SELECT id FROM offers WHERE kind='sweep' AND verified=1 ORDER BY idx DESC LIMIT 100")]
            if report['history']['first_page_source_ids']!=newest:
                raise BenchmarkError('history_invalid','First history page does not identify the newest retained sources')
            report['event_watermark_after']=client.json('/api/v1/health').get('last_event_id')
            render_path=observer.render_path if observer is not None else config.render_events
            if render_path:
                from .observe import correlate
                if observer is not None and not observer.completed:
                    report['checks']['render_latency']={'status':'failed','reason':'observer_failed'}
                else:
                    try:
                        report['checks']['render_latency']=correlate(render_path,config,ledger,histogram['offered_to_render_ms'])
                    except BenchmarkError as error:
                        report['checks']['render_latency']={'status':'failed','reason':error.code}
            functional=True
    except BenchmarkError as error:
        report['errors'].append({'code':error.code,'message':error.message})
        report['run_status']='unsupported' if error.code=='unsupported' else 'failed'
        if error.code in ('archive_invalid','audio_invalid'):
            report['checks']['archive_integrity']={'status':'failed','reason':error.code}
        if error.code=='observer_failed':
            report['checks']['render_latency']={'status':'failed','reason':error.code}
    except (Exception,KeyboardInterrupt):
        report['errors'].append({'code':'interrupted_or_internal_error','message':'Run did not complete; inspect the owned ledger'})
    finally:
        if observer is not None and not observer.closed:observer.close(grace=False)
        counts=ledger.counts();planned=config.planned_counts()
        accounted=all(row['offered']==sum(row[k] for k in ('acknowledged','rejected','queue_dropped',
            'transport_unknown','injected_gap','cancelled','invalid_ack')) for row in counts.values())
        all_offered=all(counts[k]['offered']==planned[k] for k in planned)
        complete=all_offered and all(counts[k]['verified']==planned[k] for k in planned)
        complete=complete and all(counts[k]['acknowledged']==planned[k] for k in planned)
        report['counts']=counts
        report.setdefault('audio',ledger.audio_counts())
        report['gap_samples']=ledger.gap_samples()
        report['checks']['offered_accounting']={'status':'passed' if accounted and all_offered else 'failed',
            'all_planned_inputs_offered':all_offered}
        report['checks']['complete_delivery']={'status':'passed' if complete else 'failed',
            'denominator':'all planned and offered inputs, including rejections and client-side drops'}
        report['latency']={key:value.report() for key,value in histogram.items()}
        summary=report['latency']['history_summary_ms']
        if summary['count']:
            matched=config.mode=='history' and config.sweeps==1000000 and config.points==290 and complete
            report['checks']['history_summary_p95']={'status':('passed' if summary['p95_upper_ms']<=500 else 'failed')
                if matched else 'unmeasured',
                'measured_p95_upper_ms':summary['p95_upper_ms'],'target_ms':500,
                'reference_workload_matched':matched,'scope':'actual host, not Pi acceptance'}
        report['resources']=sampler.report();report['elapsed_seconds']=time.monotonic()-started
        report['pi_acceptance_claimed']=False;report['hardware_tested']=False
        report['accelerated_source_time']=config.speed!=1
        if functional:
            expected_faults=config.scenario in ('fault','overload')
            success=accounted and all_offered and not any(row['invalid_ack'] for row in counts.values()) \
                and (complete or expected_faults) \
                and (not (config.render_events or config.observe_render) or report['checks']['render_latency']['status']=='passed')
            report['run_status']='completed' if success else 'failed';status=0 if success else 1
        ledger.close()
        atomic_report(output,report)
    return report,status
