"""Pure, fail-closed evaluation of original host evidence (not simulated elapsed time)."""
from __future__ import annotations
import math

CHECKPOINTS = ('baseline','guard','event','receiver','source_search','ui_quit',
               'usb_analyzer','usb_receiver','sleep','history_restore')


def integer(value):
    return type(value) is int and 0 <= value < 2**64


def percentile(values):
    return sorted(values)[math.ceil(len(values)*.95)-1] if values else None


def metric(values, missing, maximum, minimum=1):
    p95 = percentile(values)
    return dict(status='passed' if not missing and len(values)>=minimum and p95 is not None and p95<=maximum else 'failed',
                samples=len(values), missing=sorted(missing), p95_ms=p95, target_ms=maximum)


def native_metrics(commits, events, *, run_id, core_sha256, native_sha256, expected_ids):
    errors=[]
    expected=set(expected_ids)
    committed={}
    sequences=set()
    native={}
    begins={'interaction':{},'queue':{},'pan_zoom':{}}
    ends={'interaction':{},'queue':{},'pan_zoom':{}}
    frames=[]
    identity=False
    render_ids=set()
    for row in commits:
        if row.get('stage')!='committed' or row.get('kind')!='sweep': continue
        key=row.get('source_id')
        if row.get('run_id')!=run_id or row.get('component_sha256')!=core_sha256 \
                or key not in expected or key in committed or not integer(row.get('at_ns')) \
                or not integer(row.get('sequence')) or row['sequence']==0 or row['sequence'] in sequences:
            errors.append('invalid_or_duplicate_commit');continue
        sequences.add(row['sequence']);committed[key]=row
    for row in events:
        stage=row.get('stage')
        if row.get('run_id')!=run_id or not integer(row.get('at_ns')):
            errors.append('invalid_native_identity_or_clock');continue
        if stage in ('native.error','queue.error'):errors.append(str(row.get('message','native_observer_failed')))
        elif stage=='native_identity':
            if identity or row.get('component_sha256')!=native_sha256:errors.append('native_build_mismatch')
            else:identity=True
        elif stage in ('received','applied','presented'):
            key=row.get('source_id')
            original=committed.get(key)
            if original is None or row.get('sequence')!=original['sequence'] or row['at_ns']<original['at_ns']:
                errors.append('unbound_or_negative_frame');continue
            samples=native.setdefault(key,{})
            samples[stage]=min(samples.get(stage,row['at_ns']),row['at_ns'])
            if stage=='presented':
                render=row.get('render_id')
                if not integer(render) or render in render_ids:errors.append('duplicate_or_invalid_drawable')
                else:render_ids.add(render);frames.append(row)
        else:
            for name,final in [('interaction','presented'),('queue','accepted'),('pan_zoom','end')]:
                if stage not in (name+'.begin',name+'.'+final):continue
                key=row.get('request_id')
                target=begins[name] if stage.endswith('.begin') else ends[name]
                if not isinstance(key,str) or not key or key in target or len(key)>128:
                    errors.append('duplicate_or_invalid_request');break
                if name=='queue' and stage.endswith('.accepted') and (not isinstance(row.get('job_id'),str) or not row['job_id']):
                    errors.append('queue_without_job');break
                target[key]=row['at_ns']
    if not identity:errors.append('native_identity_missing')
    latencies=[];missing=[]
    for key in sorted(expected):
        source=committed.get(key);stages=native.get(key,{})
        if source is None or any(stage not in stages for stage in ('received','applied','presented')):
            missing.append(key);continue
        if not source['at_ns']<=stages['received']<=stages['applied']<=stages['presented']:
            errors.append('frame_stage_order');missing.append(key);continue
        latencies.append((stages['presented']-source['at_ns'])/1e6)
    result=dict(commit_to_presented=metric(latencies,missing,250),errors=errors)
    for name,label,maximum in [('interaction','reaction',100),('queue','queue_ack',200)]:
        absent=set(begins[name])-set(ends[name]);values=[]
        if set(ends[name])-set(begins[name]):errors.append(name+'_without_begin')
        for key,start in begins[name].items():
            end=ends[name].get(key)
            if end is not None:
                if end<start:errors.append(name+'_clock_order');absent.add(key)
                else:values.append((end-start)/1e6)
        result[label]=metric(values,absent,maximum,minimum=100)
    fps=[]
    for key,start in begins['pan_zoom'].items():
        end=ends['pan_zoom'].get(key)
        if end is None or end-start<10_000_000_000:errors.append('pan_window_incomplete');continue
        changes=0;previous=None
        for row in sorted((row for row in frames if start<=row['at_ns']<=end),key=lambda row:row['at_ns']):
            view=(row.get('viewport_lower_hz'),row.get('viewport_upper_hz'))
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in view) or view[0]>=view[1]:
                errors.append('pan_viewport_not_measured');continue
            if previous is not None and view!=previous:changes+=1
            previous=view
        fps.append(changes/((end-start)/1e9))
    if set(ends['pan_zoom'])-set(begins['pan_zoom']):errors.append('pan_without_begin')
    result['pan_zoom']=dict(status='passed' if fps and min(fps)>=30 else 'failed',
                            minimum_fps=min(fps) if fps else None,windows=len(fps),target_fps=30)
    result['status']='passed' if not errors and all(result[key]['status']=='passed' for key in
                              ('commit_to_presented','reaction','queue_ack','pan_zoom')) else 'failed'
    return result


def hardware_metrics(events, *, run_id, core_sha256, elapsed_seconds):
    from .checkpoints import assess
    from .freshness import SourceProgress
    import re
    errors=[];checkpoints=set();samples=[];proofs=[];identity=False;planned=[];references={};receipts=[];window={}
    for row in events:
        if row.get('run_id')!=run_id or row.get('component_sha256')!=core_sha256 or not integer(row.get('at_ns')):
            errors.append('hardware_identity_or_clock_mismatch');continue
        stage=row.get('stage')
        if stage=='hardware.identity':
            identity=row.get('mode')=='live' and row.get('analyzer_model')=='tinySA Ultra' \
                     and row.get('receiver_model')=='AR-DV1' and bool(row.get('analyzer_firmware')) \
                     and bool(row.get('receiver_firmware'))
            if not identity:errors.append('hardware_profile_not_confirmed')
        elif stage in ('hardware.window.begin','hardware.window.end'):
            if stage in window:errors.append('duplicate_hardware_window')
            window[stage]=row['at_ns']
        elif stage=='hardware.planned_gap':
            start,end=row.get('start_ns'),row.get('end_ns');kind=row.get('kind')
            limit=900 if kind=='sleep' else 300
            if kind in ('sleep','usb_analyzer','usb_receiver') and integer(start) and integer(end) and 0<=end-start<=limit*1_000_000_000:
                planned.append((start,end,kind))
            else:errors.append('invalid_planned_gap')
        elif stage=='hardware.error':errors.append(str(row.get('message','unknown_hardware_error')))
        elif stage in ('hardware.sample','checkpoint.sample'):
            key=row.get('id')
            if not isinstance(key,str) or not key or key in references:errors.append('invalid_sample_reference')
            else:references[key]=row
            if stage=='hardware.sample':samples.append(row)
            if row.get('unexpected_gap') or row.get('archive_error'):errors.append('unexpected_gap_or_archive_error')
        elif stage=='checkpoint.verified':receipts.append(row)
        elif stage=='hardware.source_proof':
            if row.get('status')=='passed' and row.get('commits_verified') is True and type(row.get('sweeps')) is int and row['sweeps']>0 \
                    and type(row.get('observations')) is int and row['observations']>0 and row.get('missing')==0 \
                    and re.fullmatch('[0-9a-f]{64}',str(row.get('original_sha256',''))) \
                    and row['original_sha256']==row.get('restored_sha256'):
                proofs.append(row)
            else:errors.append('originals_not_verified')
    for receipt in receipts:
        name=receipt.get('name');keys=receipt.get('evidence_ids')
        if name not in CHECKPOINTS or receipt.get('observed') is not True or not isinstance(keys,list) \
                or not keys or len(keys)>2000 or any(not isinstance(key,str) or key not in references for key in keys):
            errors.append('checkpoint_reference_invalid');continue
        selected=[references[key] for key in keys]
        if any(row['at_ns']>receipt['at_ns'] for row in selected) or selected!=sorted(selected,key=lambda row:row['at_ns']):
            errors.append('checkpoint_clock_order');continue
        ok,_=assess(name,selected)
        if ok:checkpoints.add(name)
        else:errors.append('checkpoint_transition_missing:'+name)
    missing=sorted(set(CHECKPOINTS)-checkpoints)
    if not identity:errors.append('hardware_identity_missing')
    start=window.get('hardware.window.begin');end=window.get('hardware.window.end')
    if start is None or end is None or end-start<28_800_000_000_000:errors.append('eight_real_hours_not_observed')
    if not samples:errors.append('coverage_samples_missing')
    elif len(samples)<2 or samples[-1]['at_ns']-samples[0]['at_ns']<28_800_000_000_000:
        errors.append('eight_hours_of_coverage_not_observed')
    if samples and start is not None and end is not None and (samples[0]['at_ns']>start+3_000_000_000 or samples[-1]['at_ns']<end-3_000_000_000):
        errors.append('hardware_window_not_covered')
    for before,after in zip(samples,samples[1:]):
        if after['at_ns']<=before['at_ns']:errors.append('coverage_clock_order')
        elif after['at_ns']-before['at_ns']>15_000_000_000 and not any(
                kind=='sleep' and a<=before['at_ns']+2_000_000_000 and b>=after['at_ns']-2_000_000_000 for a,b,kind in planned):
            errors.append('unobserved_coverage_interval')
    if start is not None and end is not None:
        progress=SourceProgress()
        for row in samples:
            if start<=row['at_ns']<=end:
                errors.extend(progress.observe(row,planned=planned))
                if row.get('active') is not True and not any(a<=row['at_ns']<=b for a,b,_ in planned):
                    errors.append('guard_stopped_outside_declared_gap')
    for proof in proofs:
        if any(proof[kind]<max((s.get(role+'_sources',0) for s in samples),default=0)
               for kind,role in [('sweeps','analyzer'),('observations','receiver')]):
            errors.append('observed_committed_sources_missing_from_restore')
    if not proofs:errors.append('source_proof_missing')
    if type(elapsed_seconds) not in (int,float) or not math.isfinite(elapsed_seconds) or elapsed_seconds<28_800:
        errors.append('eight_real_hours_not_observed')
    return dict(status='passed' if not errors and not missing else 'failed',
                elapsed_seconds=elapsed_seconds,missing_checkpoints=missing,errors=sorted(set(errors)),
                samples=len(samples),originals_verified=bool(proofs))
