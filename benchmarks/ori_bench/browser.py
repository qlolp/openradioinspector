"""Owned draft-project preparation and bounded passive browser lifecycle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from .client import segment
from .config import BenchmarkError


def resolve_node(config):
    executable=shutil.which(config.observer_node)
    if not executable:
        raise BenchmarkError('unsupported','Configured local observer Node executable is unavailable')
    return executable


class OwnedObserver:
    def __init__(self,config,client,output):
        self.config,self.client,self.output=config,client,output
        self.process=None;self.log=None;self.closed=False
        self.render_path=output/'render-events.ndjson'
        self.ready_path=output/'render-ready.json'
        self.stop_path=output/'render-stop'
        self.setup=None;self.completed=False

    def start(self):
        config=self.config
        project=self.client.json('/api/v1/monitoring-projects',method='POST',status=201,payload={
            'name':'Synthetic benchmark '+config.run_id,'site':'Disposable benchmark-owned node',
            'session_id':config.session_id})
        identifier=project.get('id')
        if not isinstance(identifier,str) or not 0<len(identifier)<=128 \
                or project.get('state')!='draft' or project.get('autostart') is not False \
                or project.get('active_session_id')!=config.session_id \
                or project.get('analyzer_stream_id')!=config.spectrum_id \
                or project.get('receiver_stream_id')!=config.receiver_id:
            raise BenchmarkError('unsafe_target','Owned observer project must be new, draft, and bound to its synthetic streams')
        guard=self.client.json('/api/v1/monitoring-projects/'+segment(identifier)+'/status')
        if guard.get('active') is not False or guard.get('receiver_busy') is not False:
            raise BenchmarkError('unsafe_target','Owned observer project is unexpectedly active')
        script=Path(__file__).resolve().parents[1]/'observe-render.mjs'
        args=[resolve_node(config),str(script),'--url',config.base_url.rstrip('/')+'/modern/',
            '--output',str(self.render_path),'--run-id',config.run_id,'--clients',str(config.observer_clients),
            '--duration-seconds',str(min(86400,config.duration_seconds/config.speed+config.drain_seconds+60)),
            '--session-id',config.session_id,'--project-id',identifier,'--ready-file',str(self.ready_path),
            '--stop-file',str(self.stop_path)]
        self.log=(self.output/'observer.log').open('x',encoding='utf-8')
        try:
            self.process=subprocess.Popen(args,stdout=self.log,stderr=subprocess.STDOUT,
                start_new_session=os.name!='nt')
            deadline=time.monotonic()+config.observer_ready_timeout
            while not self.ready_path.exists():
                if self.process.poll() is not None:
                    raise BenchmarkError('observer_failed','Passive observer exited before readiness')
                if time.monotonic()>=deadline:
                    raise BenchmarkError('observer_failed','Passive clients did not become ready within the bounded deadline')
                time.sleep(.05)
            if self.ready_path.stat().st_size>4096:
                raise BenchmarkError('observer_failed','Observer readiness record is too large')
            ready=json.loads(self.ready_path.read_bytes())
            if ready.get('ready') is not True or ready.get('clients')!=config.observer_clients \
                    or ready.get('project_id')!=identifier or ready.get('session_id')!=config.session_id:
                raise BenchmarkError('observer_failed','Observer readiness does not match the owned source context')
            self.setup={'project_id':identifier,'state':'draft','autostart':False,'guard_active':False,
                'session_id':config.session_id,'clients':config.observer_clients,'readiness':ready,
                'observer_pid':self.process.pid,'scope':'new owned session and local client view only'}
            return self.setup
        except BenchmarkError:
            self.close(grace=False);raise
        except (OSError,ValueError,TypeError):
            self.close(grace=False)
            raise BenchmarkError('observer_failed','Owned browser preparation failed') from None

    def close(self,*,grace=True):
        if self.closed:return self.completed
        self.closed=True
        if self.process is not None:
            if grace and self.process.poll() is None:time.sleep(self.config.observer_grace_seconds)
            self.stop_path.touch(exist_ok=True)
            try:self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if os.name!='nt':
                    try:os.killpg(self.process.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                else:self.process.terminate()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
            self.completed=self.process.returncode==0 and self.render_path.is_file()
        if self.log is not None:self.log.close()
        return self.completed
