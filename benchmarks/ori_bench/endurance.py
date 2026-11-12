"""Accelerated sampled source timeline across real owned Rust Core restarts.

Default: seven logical days sampled hourly, including 1,680 seconds of PCM.
This does not establish continuous audio, real-time endurance or RF acceptance.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sys
import time

from . import verify
from .client import Client,HTTPFailure,JSON_LIMIT,segment
from .config import BenchmarkError,Config
from .endurance_node import OwnedCandidate
from .endurance_proof import EventProof,checkpoint
from .ledger import Ledger
from .metrics import Histogram
from .runner import LATENCIES,atomic_report,output_directory,run_load
from .source import encode,identifier,measurement_hash,observation,session as session_payload,stamp,sweep
from tools.modernization.api_parity import load_json

GUARD_PAGE = 16


class StrictClient(Client):
    def json(self,path,*,method='GET',payload=None,status=200):
        try:
            with self.open(path,method=method,payload=payload) as response:
                if response.status!=status:raise HTTPFailure(response.status)
                value = load_json(response.read(JSON_LIMIT+1),JSON_LIMIT)
                if not isinstance(value,dict) or value.get('contract')!='openradioinspector-platform' \
                        or type(value.get('contract_version')) is not int or value['contract_version']!=1 \
                        or 'data' not in value:
                    raise BenchmarkError('invalid_response','Original version1 envelope is missing')
                return value['data']
        except BenchmarkError as error:
            error.operation = method+' '+path
            raise
        except (OSError,TimeoutError) as error:
            failed = BenchmarkError('transport_error','HTTP body transport did not complete')
            failed.operation = method+' '+path
            raise failed from error


def lifecycle_config(base_url,output,run_id,samples,*,points=4096,speed=86400):
    if type(samples) is not int or not 1<=samples<=168:
        raise BenchmarkError('invalid_config','Hourly sample count must be 1..168')
    return Config(base_url=base_url,output_dir=output,run_id=run_id,scenario='lifecycle',
                  duration_seconds=samples*3600,lifecycle_step_seconds=3600,speed=speed,
                  points=points,history_repeats=1,drain_seconds=120,
                  endpoint_label='owned-immutable-rust-candidate')


def fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def candidate_identity(capabilities,health):
    candidate = capabilities.get('candidate')
    if health.get('backend')!='rust-candidate' or health.get('status')!='ok' \
            or capabilities.get('storage_mode')!='sqlite' or not isinstance(candidate,dict) \
            or candidate.get('mode')!='replay':
        raise BenchmarkError('candidate_unavailable','Public identity is not the requested healthy replay Rust candidate')


def original_json(client,path):
    try:return client.json(path)
    except HTTPFailure as error:
        if error.status==404:
            failure = BenchmarkError('archive_invalid','An acknowledged original is unavailable by its exact public ID')
            failure.operation = 'GET '+path
            raise failure from error
        raise


def idle_workers(rows,config):
    if not isinstance(rows,list) or len(rows)!=2 \
            or {row.get('stream_id') for row in rows if isinstance(row,dict)}!={config.spectrum_id,config.receiver_id}:
        raise BenchmarkError('unsafe_target','Public worker inventory does not match the owned streams')
    for row in rows:
        if row.get('session_id')!=config.session_id or row.get('alive') is not False \
                or any(type(row.get(key)) is not int or row[key]!=0 for key in ('measurements','commands_completed')) \
                or row.get('started_at') is not None or row.get('last_measurement_id') is not None:
            raise BenchmarkError('unsafe_target','An unexpected device worker is active or has performed I/O')
    return rows


def expected_recovery(session):
    value = copy.deepcopy(session)
    for stream in value['streams']:
        stream['state'] = 'disconnected'
    return value


def export_outcome(epochs):
    if len(epochs)!=3:
        raise BenchmarkError('archive_invalid','Export proof does not cover all three owned epochs')
    hashes = [epoch.get('ndjson_sha256') for epoch in epochs if epoch.get('status')=='passed']
    if any(not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value)
           for value in hashes) or len(set(hashes))>1:
        raise BenchmarkError('archive_invalid','Original NDJSON export bytes changed across restart')
    return dict(status='passed' if len(hashes)==3 else 'unavailable',epochs=epochs)


def inactive(client,project,config,*,preflight=False):
    base = '/api/v1/monitoring-projects/'+segment(project)
    status = client.json(base+'/status')
    if status.get('active') is not False or status.get('receiver_busy') is not False \
            or status.get('project',{}).get('state')!='draft' \
            or status['project'].get('autostart') is not False:
        raise BenchmarkError('unsafe_target','Owned Guard project is not an inactive draft')
    workers = idle_workers(client.json('/api/v1/workers'),config)
    result = dict(active=False,receiver_busy=False,project=status['project'],active_workers=0,workers=workers)
    # Observe preflight before production: the candidate's hardware-readiness
    # estimator can read128 full sweeps. Do not turn it into an unbounded bulk
    # query or manufacture a successful readiness result after ingestion.
    if preflight:result['preflight'] = client.json(base+'/preflight')
    return result


def guard_history(client,config,project):
    base = '/api/v1/monitoring-projects/'+segment(project)+'/history'
    after = None
    remaining = config.planned_counts()['sweep']-1
    pages,maximum,display_points = 0,0,0
    digest = hashlib.sha256()
    while True:
        page = client.json(base+'?limit='+str(GUARD_PAGE)+('&after='+segment(after) if after else ''))
        rows = page.get('items')
        pages += 1
        if not isinstance(rows,list) or len(rows)>GUARD_PAGE or pages>168:
            raise BenchmarkError('history_invalid','Guard history exceeded its bounded page contract')
        maximum = max(maximum,len(rows))
        for item in rows:
            if remaining<0 or item.get('id')!=identifier(config,'sweep',remaining) \
                    or item.get('original_point_count')!=config.points \
                    or item.get('display_only') is not True or len(item.get('levels',[]))>1024:
                raise BenchmarkError('history_invalid','Guard history lost or repeated an original source identity')
            display_points = max(display_points,len(item['levels']))
            digest.update(item['id'].encode('utf-8')+b'\n')
            if remaining in (0,config.planned_counts()['sweep']-1):
                original = original_json(client,base+'/'+segment(item['id']))
                if measurement_hash('sweep',original)!=measurement_hash('sweep',sweep(config,remaining)):
                    raise BenchmarkError('archive_invalid','Guard original source differs from the offered measurement')
            remaining -= 1
        cursor = page.get('next_cursor')
        if cursor is None:break
        if not isinstance(cursor,str) or not cursor or len(cursor)>4096 or cursor==after or not rows:
            raise BenchmarkError('history_invalid','Guard history cursor failed to advance')
        after = cursor
    if remaining!=-1:raise BenchmarkError('history_invalid','Guard history omitted an original source')
    return dict(verified=config.planned_counts()['sweep'],id_sha256=digest.hexdigest(),
                pages=pages,max_page_rows=maximum,max_display_points=display_points)


def reverify(client,config,ledger,output,capabilities):
    # Reset only disposable verification flags in the original offered ledger.
    # No offered ID, outcome, source timestamp or canonical row is rewritten.
    with ledger.db:ledger.db.execute('UPDATE offers SET verified=0,listed=0')
    histogram = {name:Histogram() for name in LATENCIES}
    history = verify.history(client,config,capabilities,histogram,ledger)
    digest = hashlib.sha256()
    counts = {}
    for kind,suffix,make in (('sweep','sweeps',sweep),('observation','observations',observation)):
        counts[kind] = 0
        for row in ledger.rows(kind):
            value = original_json(client,client.session_path(config)+'/'+suffix+'/'+segment(row['id']))
            if measurement_hash(kind,value)!=measurement_hash(kind,make(config,row['idx'])) \
                    or not ledger.verified(row['id']):
                raise BenchmarkError('archive_invalid','Original raw source differs from the offered measurement')
            digest.update(encode(dict(kind=kind,value=value))+b'\n')
            counts[kind] += 1
    totals = client.json(client.session_path(config)+'/analytics/summary')
    if any(type(totals[table]['count']) is not int or totals[table]['count']!=counts[kind]
           for table,kind in (('sweeps','sweep'),('observations','observation'))):
        raise BenchmarkError('archive_invalid','Public archive totals contain missing or foreign sources')
    archive = dict(status='passed',verification='exact_public_raw_ids_and_archive_counts',
                   source_sha256=digest.hexdigest(),source_counts=counts)
    if capabilities.get('features',{}).get('session_archive_export_ndjson') is True:
        with ledger.db:ledger.db.execute("UPDATE offers SET verified=0 WHERE kind IN ('sweep','observation')")
        archive['ndjson_export'] = verify.archive(client,config,ledger)
    else:
        archive['ndjson_export'] = dict(status='unavailable',reason='Candidate does not advertise NDJSON export')
    try:audio = verify.audio(client,config,ledger,output)
    except HTTPFailure as error:
        if error.status==404:
            raise BenchmarkError('audio_invalid','An acknowledged original audio artifact cannot be read') from error
        raise
    counts = ledger.counts()
    for kind,expected in config.planned_counts().items():
        if counts[kind]['offered']!=expected or counts[kind]['acknowledged']!=expected \
                or counts[kind]['verified']!=expected:
            raise BenchmarkError('archive_invalid','An offered source was lost after restart')
    if audio['all_offered_pcm_preserved'] is not True:
        raise BenchmarkError('audio_invalid','Original closed PCM was lost after restart')
    return dict(history=history,archive=archive,audio=audio,counts=counts)


def produce(client,config,result,prepare):
    """Compose the unchanged original offered-input producer, not a new source."""
    capabilities = client.json('/api/v1/capabilities')
    features = capabilities.get('features',{})
    if capabilities.get('persistent_archive') is not True \
            or any(features.get(key) is not True for key in ('measurement_batch_ingest','audio_segment_ingest')):
        raise BenchmarkError('candidate_unavailable','Candidate lacks persistent batch/audio ingestion')
    health = client.json('/api/v1/health')
    candidate_identity(capabilities,health)
    projects = client.json('/api/v1/monitoring-projects?limit=1')
    if health.get('sessions')!=0 or health.get('last_event_id')!=0 \
            or projects.get('items')!=[] or projects.get('next_cursor') is not None \
            or client.json('/api/v1/workers')!=[]:
        raise BenchmarkError('unsafe_target','Replay archive is not fresh with inactive instrument ownership')
    created = client.json('/api/v1/sessions',method='POST',payload=session_payload(config),status=201)
    if created.get('id')!=config.session_id \
            or {row['id'] for row in created.get('streams',[])}!={config.spectrum_id,config.receiver_id}:
        raise BenchmarkError('invalid_session','Creation did not establish the exact owned source context')
    prepare()
    directory = output_directory(config.output_dir)
    ledger = Ledger(directory/'offers.sqlite')
    result.update(producer='benchmarks.ori_bench.runner.run_load',workload=config.public(),
                  capabilities=capabilities)
    try:
        histogram = {name:Histogram() for name in LATENCIES}
        production = run_load(config,ledger,histogram)
        for kind,expected in config.planned_counts().items():
            counts = ledger.counts()[kind]
            if counts['offered']!=expected or counts['acknowledged']!=expected:
                raise BenchmarkError('producer_failed','Original producer did not deliver every planned offered source')
        verified = reverify(client,config,ledger,directory,capabilities)
        result.update(run_status='completed',production=production,verification=verified,
                      counts=ledger.counts(),latency={key:value.report() for key,value in histogram.items()})
        atomic_report(directory,result)
        return ledger
    except BaseException as error:
        result.update(run_status='failed',counts=ledger.counts(),
                      errors=[dict(code=getattr(error,'code','incomplete_production'),error_type=type(error).__name__)])
        try:atomic_report(directory,result)
        finally:ledger.close()
        raise


def run_endurance(output,*,candidate=None,samples=168,run_id='sampled-week',points=4096,speed=86400):
    initial = lifecycle_config('http://127.0.0.1:1',Path(output)/'producer',run_id,samples,points=points,speed=speed)
    output = output_directory(output).resolve(strict=True)
    initial = replace(initial,output_dir=output/'producer')
    report = dict(report_version=1,run_status='unavailable',successful=False,
                  scenario='accelerated seven-day sampled timeline' if samples==168 else 'accelerated sampled timeline smoke',
                  workload=initial.public(),hardware_tested=False,pi_acceptance_claimed=False,
                  physical_endurance='unmeasured',continuous_seven_day_audio='not_offered',
                  audio_pcm_seconds=samples*10,seven_day_sampled_timeline_verified=False,
                  internal_working_memory_acceptance='unmeasured',guard_arming='not_requested',
                  source_timeline=dict(start_utc=stamp(0),window_end_utc=stamp(initial.duration_us),
                                       last_sample_utc=stamp((samples-1)*3600*1_000_000),
                                       cadence_seconds=3600,core_clock_advanced=False),
                  restarts=[],errors=[],owned_nodes=[])
    core = proof = ledger = None
    status = 2
    try:
        if candidate is None:
            raise BenchmarkError('candidate_unavailable','Supply --candidate or ORI_CORE_ENDURANCE_BINARY explicitly')
        candidate = Path(candidate)
        if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate,os.X_OK):
            raise BenchmarkError('candidate_unavailable','Requested immutable candidate executable is unavailable')
        if os.name!='posix' or sys.platform not in ('linux','darwin'):
            raise BenchmarkError('candidate_unavailable','Owned candidate process backend is unavailable on this platform')
        core = OwnedCandidate(candidate.resolve(strict=True),output)
        report.update(candidate_sha256=core.sha256,candidate_path=str(core.binary))
        config = replace(initial,base_url=core.start())
        client = StrictClient(config)
        report['producer_report'] = 'producer/report.json'
        report['production'] = producer = {}
        def prepare_project():
            created = client.json('/api/v1/monitoring-projects',method='POST',status=201,
                                  payload=dict(name='Owned sampled lifecycle',site='Synthetic only; no RF coverage claim',
                                               session_id=config.session_id))
            report['project_id'] = created['id']
            report['guard_before'] = inactive(client,created['id'],config,preflight=True)
        ledger = produce(client,config,producer,prepare_project)
        project = report['guard_before']['project']
        project_id = project['id']
        session = client.json(client.session_path(config)+'/start',method='POST',payload={})
        if session.get('state')!='running':
            raise BenchmarkError('session_invalid','Metadata-only session start did not return running')
        session_digest = fingerprint(expected_recovery(session))
        report['session_running_without_workers'] = True
        report['session_before'] = session
        report['expected_recovered_session_sha256'] = session_digest
        report['runtime_recovery_expected'] = dict(stream_states='disconnected',session_state='running',
                                                   automatic_worker_restart=False)
        proof = EventProof(output/'events.sqlite')
        baseline = checkpoint(client,proof,record=True)
        expected_types = {'session.created':1,'session.started':1,
                          'sweep.received':samples,'observation.received':samples,'audio_segment.received':samples}
        if baseline['types']!=expected_types:
            raise BenchmarkError('events_changed','Actual event counts differ from exact public operations')
        report['events_before'] = baseline
        report['guard_history_before'] = guard_history(client,config,project_id)
        archive_digest = producer['verification']['archive']['source_sha256']
        pcm_digest = producer['verification']['audio']['retained_pcm_sha256']
        for index in range(2):
            downtime = time.monotonic()
            core.close()
            config = replace(config,base_url=core.start())
            client = StrictClient(config)
            resumed = dict(epoch=index+1,observed_service_interruption_seconds=time.monotonic()-downtime,
                           interruption_scope='owned service downtime; no RF coverage measurement')
            current = original_json(client,client.session_path(config))
            if current.get('state')!='running' or fingerprint(current)!=session_digest:
                raise BenchmarkError('session_changed','Original running session metadata changed across restart')
            resumed['session_running_without_workers'] = True
            resumed['guard'] = inactive(client,project_id,config)
            if fingerprint(resumed['guard']['project'])!=fingerprint(project):
                raise BenchmarkError('project_changed','Original inactive Guard project changed across restart')
            resumed['events'] = checkpoint(client,proof,expected_watermark=baseline['last_event_id'])
            if resumed['events']!=baseline:
                raise BenchmarkError('events_changed','Restart changed or duplicated an original event')
            capabilities = client.json('/api/v1/capabilities')
            verification = output/('verify-'+str(index+1));verification.mkdir(mode=0o700)
            resumed['sources'] = reverify(client,config,ledger,verification,capabilities)
            if resumed['sources']['archive']['source_sha256']!=archive_digest \
                    or resumed['sources']['audio']['retained_pcm_sha256']!=pcm_digest:
                raise BenchmarkError('archive_invalid','Restart changed original source bytes')
            resumed['guard_history'] = guard_history(client,config,project_id)
            if resumed['guard_history']!=report['guard_history_before']:
                raise BenchmarkError('history_invalid','Restart changed bounded Guard history identity')
            report['restarts'].append(resumed)
        exports = export_outcome([producer['verification']['archive']['ndjson_export'],
                                  *(epoch['sources']['archive']['ndjson_export'] for epoch in report['restarts'])])
        available = exports['status']=='passed'
        report.update(successful=available,run_status='completed' if available else 'unavailable',
                      seven_day_sampled_timeline_verified=samples==168,
                      source_and_event_recovery_idempotent=True,
                      sampled_lifecycle_proof='passed',
                      ndjson_export=exports,
                      public_working_tail=dict(summary_limit=100,
                                               observed_summary_rows=len(producer['verification']['history']['first_page_source_ids']),
                                               guard_history_limit=GUARD_PAGE,
                                               observed_guard_history_rows=report['guard_history_before']['max_page_rows'],
                                               event_limit=8,observed_event_rows=baseline['max_page_rows'],
                                               internal_memory_acceptance='unmeasured'))
        status = 0 if available else 2
        if not available:
            report['errors'].append(dict(code='required_route_unavailable',
                                        message='Lifecycle proof completed; NDJSON export remains unavailable'))
    except HTTPFailure as error:
        report['run_status'] = 'unavailable' if error.status in (404,501,503) else 'failed'
        report['errors'].append(dict(code='required_route_unavailable',http_status=error.status,
                                     operation=getattr(error,'operation',None)))
        status = 2 if report['run_status']=='unavailable' else 1
    except BenchmarkError as error:
        report['run_status'] = 'unavailable' if error.code=='candidate_unavailable' else 'failed'
        report['errors'].append(dict(code=error.code,message=error.message,
                                     operation=getattr(error,'operation',None)))
        status = 2 if report['run_status']=='unavailable' else 1
    except (Exception,KeyboardInterrupt) as error:
        report['run_status'] = 'failed'
        report['errors'].append(dict(code='incomplete_run',error_type=type(error).__name__))
        status = 1
    finally:
        for resource in (ledger,proof):
            if resource is not None:
                try:resource.close()
                except BaseException as error:
                    report['errors'].append(dict(code='proof_close_failed',error_type=type(error).__name__))
                    report.update(successful=False,run_status='failed',seven_day_sampled_timeline_verified=False,
                                  sampled_lifecycle_proof='failed',source_and_event_recovery_idempotent=False)
                    status = 1
        if core is not None:
            try:
                core.close()
                core.unchanged()
                report['immutable_binary'] = True
            except BaseException as error:
                report['errors'].append(dict(code='owned_close_failed',error_type=type(error).__name__))
                report.update(successful=False,run_status='failed',seven_day_sampled_timeline_verified=False,
                              sampled_lifecycle_proof='failed',source_and_event_recovery_idempotent=False)
                status = 1
            report['owned_nodes'] = core.records
        if not report['successful'] and report.get('sampled_lifecycle_proof')!='passed':
            report['seven_day_sampled_timeline_verified'] = False
        atomic_report(output,report)
    return report,status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,default=os.environ.get('ORI_CORE_ENDURANCE_BINARY'))
    parser.add_argument('--samples',type=int,default=168)
    parser.add_argument('--run-id',default='sampled-week')
    parser.add_argument('--points',type=int,default=4096)
    parser.add_argument('--speed',type=float,default=86400)
    args = parser.parse_args(argv)
    try:
        report,status = run_endurance(args.output_dir,candidate=args.candidate,samples=args.samples,
                                      run_id=args.run_id,points=args.points,speed=args.speed)
        print(encode(dict(run_status=report['run_status'],successful=report['successful'],
                          report=str(args.output_dir.absolute()/'report.json'))).decode('utf-8'))
        return status
    except BenchmarkError as error:
        print(encode(dict(run_status='failed',code=error.code,message=error.message)).decode('utf-8'))
        return 2


if __name__=='__main__':sys.exit(main())
