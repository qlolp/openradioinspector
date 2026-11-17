"""Sampled owned-process RSS; an absent measurement cannot satisfy a memory gate."""
from __future__ import annotations
import os
from pathlib import Path
import re
import subprocess
import sys
import math


def parse_darwin_processes(text):
    result={}
    for line in text.splitlines():
        fields=line.split(None,8)
        if len(fields)!=9 or not fields[0].isdecimal() or not fields[1].isdecimal() or not fields[7].isdecimal():
            raise ValueError('Incomplete Darwin process snapshot')
        pid,parent,rss=int(fields[0]),int(fields[1]),int(fields[7])*1024
        if pid in result or fields[2] not in ('Mon','Tue','Wed','Thu','Fri','Sat','Sun') \
                or fields[3] not in ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec') \
                or not fields[4].isdecimal() or not 1<=int(fields[4])<=31 \
                or re.fullmatch(r'[0-9]{2}:[0-9]{2}:[0-9]{2}',fields[5]) is None \
                or re.fullmatch(r'[0-9]{4}',fields[6]) is None:
            raise ValueError('Ambiguous Darwin process identity')
        result[pid]=dict(parent=parent,start=' '.join(fields[2:7]),rss=rss,executable=fields[8])
    if not result:raise ValueError('Empty Darwin process snapshot')
    return result


def darwin_processes():
    result=subprocess.run(['/bin/ps','-ww','-axo','pid=,ppid=,lstart=,rss=,comm='],
        capture_output=True,text=True,timeout=5,check=True,env=dict(os.environ,LC_ALL='C'))
    if len(result.stdout)>8*1024**2:raise ValueError('Process snapshot exceeds diagnostic budget')
    return parse_darwin_processes(result.stdout)


def owned_measurement_pids(binary,node_owner,window_owner):
    # Retained owners provide the scope. PIDs found below are read-only sampling
    # targets, never authority for signals, shutdown or adoption.
    roots={}
    for label,owner in [('background',node_owner),('native_ui',window_owner)]:
        state=owner.status() if owner else None
        pid=state.get('child_pid') if state else None
        if type(pid) is int and pid>0:roots[label]=pid
    if 'background' not in roots:return roots
    if sys.platform=='darwin':processes=darwin_processes()
    else:
        from ..metrics import linux_processes
        processes=linux_processes() or {}
    children={roots['background']}
    while True:
        additions={pid for pid,row in processes.items() if row['parent'] in children}-children
        if not additions:break
        children.update(additions)
    expected=Path(binary).resolve(strict=True);found=[]
    for pid in children:
        try:
            executable=processes.get(pid,{}).get('executable') if sys.platform=='darwin' else os.readlink('/proc/'+str(pid)+'/exe')
            if executable and Path(executable).resolve()==expected:found.append(pid)
        except OSError:continue
    if len(found)==1:roots['core']=found[0]
    return roots


def assess_resources(value,*,minimum_seconds=0,required=('core','native_ui','background')):
    limits={'core':256*1024**2,'native_ui':1024**3,'background':1536*1024**2}
    checks={}
    for name in required:
        limit=limits[name]
        row=value.get('process_trees',{}).get(name,{})
        peak=row.get('peak_tree_rss_bytes')
        valid=value.get('status')=='measured' and type(row.get('samples')) is int and row['samples']>0 \
            and row.get('missing_samples')==0 and type(peak) is int and 0<peak<=limit
        if minimum_seconds:
            interval=value.get('interval_seconds');first=row.get('first_sample_ns');last=row.get('last_sample_ns')
            gap=row.get('max_sample_gap_ns')
            valid=bool(valid and type(interval) in (int,float) and math.isfinite(interval) and 0<interval<=5
                and row['samples']>=max(2,int(.9*minimum_seconds/interval))
                and type(first) is int and type(last) is int and last-first>=minimum_seconds*1e9
                and type(gap) is int and gap<=max(2,3*interval)*1e9)
        checks[name]=dict(status='passed' if valid else 'failed',peak_rss_bytes=peak,limit_bytes=limit,
                         samples=row.get('samples',0),missing_samples=row.get('missing_samples'))
    return dict(status='passed' if checks and all(c['status']=='passed' for c in checks.values()) else 'failed',checks=checks,
        scope='Sampled RSS of the owned packaged core, native UI and replay core/adapter tree; no physical service acceptance')
