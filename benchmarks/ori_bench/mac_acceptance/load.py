"""Thirty real minutes against the supplied signed app, not a separate embedding."""
from __future__ import annotations
from dataclasses import replace
from contextlib import closing
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

from ..client import Client
from ..config import Config,BenchmarkError
from ..endurance_node import ROOT,publish_stop
from ..macos_rc import RCHistoryAndBackup,host
from ..runner import atomic_report,output_directory,run
from ..source import encode,session,sweep,observation,identifier,measurement_hash
from .evidence import bundle_identity,private_json,read_json_lines,verify_host
from .node import PackageNode
from .proof import native_metrics
from .queue_probe import QueueProbe


class Events:
    def __init__(self,path,run_id):
        self.file=Path(path).open('xb');os.chmod(path,0o600)
        self.run_id=run_id;self.lock=threading.Lock()
    def emit(self,stage,fields=None):
        row=dict(v=1,run_id=self.run_id,stage=stage,at_ns=time.monotonic_ns())
        row.update(fields or {})
        with self.lock:self.file.write(encode(row)+b'\n');self.file.flush()
    def close(self):
        with self.lock:self.file.flush();os.fsync(self.file.fileno());self.file.close()


class NativeWindow:
    def __init__(self,app,root,profile):
        self.app=Path(app);self.root=Path(root);self.profile=profile;self.owner=None
        self.registry=self.root/'ui-process-registry';self.registry.mkdir(mode=0o700)
        self.log=(self.root/'native-process.log').open('xb')
    def start(self):
        from tests.release_process_ownership import OwnedCommand
        executable=self.app/'Contents/MacOS/OpenRadioInspectorRC'
        environment={key:value for key,value in os.environ.items() if not key.startswith(('ORI_RUNTIME_','PYTHON','DYLD_'))}
        environment['PYTHONPATH']=str(ROOT)
        environment['PYTHONDONTWRITEBYTECODE']='1'
        environment['PYTHONNOUSERSITE']='1'
        self.owner=OwnedCommand.start([str(executable),'--acceptance-profile',str(self.profile)],
            environment=environment,registry=self.registry,root=ROOT,log=self.log,shutdown_timeout=15)
    def finish_service(self,run_id,*,seconds=150):
        publish_stop(self.root/'service-stop.request',run_id)
        receipt=wait_json(self.root/'native-service-stopped.json',lambda row:row.get('run_id')==run_id,seconds=seconds)
        if any(receipt.get(key) is not True for key in ('successful','resources_released','registration_removed')):
            raise BenchmarkError('service_registration_required','Core stop and dedicated LaunchAgent removal were not both confirmed')
        return receipt
    def stop(self,run_id,*,required=True):
        if self.owner is None:
            self.log.close();return
        try:
            publish_stop(self.root/'ui-stop.request',run_id)
            code=self.owner.wait(15);self.owner.release(15);self.owner=None
            if code!=0 and required:raise BenchmarkError('native_failed','Native app did not exit successfully')
            receipts=list(self.root.glob('native-finished-*.json'))
            if required and (not receipts or any(json.loads(p.read_text()).get('successful') is not True for p in receipts)):
                raise BenchmarkError('native_incomplete','Native trace did not finish/flush')
        except BaseException:
            if self.owner is not None:
                self.owner.stop(15);self.owner=None
            if required:raise
        finally:self.log.close()


def wait_json(path,predicate,*,seconds=60,owner=None):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if owner is not None and owner.owner is not None:
            status=owner.owner.status()
            if status and status.get('phase') in ('failed','exited','released','quiescent'):
                raise BenchmarkError('native_unavailable','Native app exited before readiness; see native-process.log')
        if path.is_file() and not path.is_symlink() and path.stat().st_size<=65536:
            try:value=json.loads(path.read_bytes())
            except (ValueError,UnicodeError):value=None
            if value is not None and predicate(value):return value
        time.sleep(.05)
    raise BenchmarkError('native_unavailable','Native display/stream readiness was not observed; check screen-recording permission and log')


def budget(config,output):
    counts=config.planned_counts()
    size=len(encode(sweep(config,0)))*counts['sweep']+len(encode(observation(config,0)))*counts['observation']
    needed=size*8+512*1024**2
    free=shutil.disk_usage(output).free
    if free<needed:raise BenchmarkError('insufficient_space','Active/backup/restore and diagnostics do not fit: need '+str(needed)+' bytes')
    return dict(estimated_peak_bytes=needed,free_bytes=free,source_bytes=size,automatic_deletion=False)


def restored_ledger(original,destination):
    """Reset only a separate derived copy, retaining the producer's evidence."""
    from ..ledger import Ledger
    if Path(destination).exists():raise BenchmarkError('existing_evidence','Restore ledger already exists')
    with closing(sqlite3.connect(Path(original).resolve().as_uri()+'?mode=ro',uri=True)) as source:
        with closing(sqlite3.connect(destination)) as copy:source.backup(copy)
    os.chmod(destination,0o600)
    ledger=Ledger(Path(destination),existing=True)
    ledger.db.execute('UPDATE offers SET verified=0');ledger.db.commit()
    return ledger


def complete_restore(config,proof):
    return proof.get('status')=='passed' and proof.get('source_counts')=={
        key:config.planned_counts()[key] for key in ('sweep','observation')}


def closed_native_evidence(config,node,native,controller,warm_ids,identity):
    commits=[row for row in read_json_lines(node.receipts/'core.jsonl')
             if row.get('session_id')==config.session_id]
    rows=[];warm=set(warm_ids)
    for path in native.glob('native-*.jsonl'):
        rows.extend(row for row in read_json_lines(path) if row.get('source_id') not in warm)
    if controller.exists():rows.extend(read_json_lines(controller))
    return native_metrics(commits,rows,run_id=config.run_id,core_sha256=identity['core_sha256'],
        native_sha256=identity['native_sha256'],
        expected_ids=[identifier(config,'sweep',i) for i in range(config.planned_counts()['sweep'])])


def run_load(app,manifest,output,*,smoke_seconds=None):
    root=output_directory(output).resolve(strict=True)
    report=dict(schema_version=1,profile='macos-final-load',successful=False,point2_passed=False,
                point4_passed=False,hardware_tested=False,errors=[],host=host(),
                started_at=datetime.now(timezone.utc).isoformat(),smoke_only=smoke_seconds is not None,
                components={key:{'status':'not_run'} for key in
                    ('production','native','online_backup','final_backup','restore','cleanup')})
    node=window=background=queue=events=awake=None
    config=native=identity=None;warm_ids=[];produced=None
    status=1
    try:
        identity=bundle_identity(app,manifest);report['identity']=identity
        if not verify_host(report['host']):raise BenchmarkError('wrong_host','Run on the target Apple Silicon Mac/macOS27.3 with16+GiB')
        if smoke_seconds is not None and (type(smoke_seconds) not in (int,float) or not 1<=smoke_seconds<=60):
            raise BenchmarkError('invalid_config','Smoke is explicitly1..60seconds and never acceptance')
        subprocess.run(['codesign','--verify','--strict','--deep',str(app)],check=True,timeout=60)
        run_id='macload-'+uuid.uuid4().hex
        report['run_id']=run_id
        config=Config('http://127.0.0.1:1',root/'producer',run_id=run_id,workload_profile='macos-rc1',
                      duration_seconds=1800 if smoke_seconds is None else smoke_seconds)
        report['archive_budget']=budget(config,root)
        node_dir=root/'node';node_dir.mkdir(mode=0o700)
        node=PackageNode(Path(app)/'Contents/Helpers/ori-core',node_dir,run_id)
        config=replace(config,base_url=node.start(),pids={'owned_node_tree':node.owner.status()['child_pid']})
        client=Client(config)
        native=root/'native';native.mkdir(mode=0o700)
        warm=replace(config,run_id=run_id+'-warmup',output_dir=root/'unused-warmup')
        client.json('/api/v1/sessions',method='POST',payload=session(warm),status=201)
        profile=native/'profile.json'
        private_json(profile,dict(version=1,run_id=run_id,phase='load',output_dir=str(native),
            expected_ui_sha256=identity['native_sha256'],core_url=config.base_url,
            session_id=config.session_id,warmup_session_id=warm.session_id))
        events=Events(root/'controller.jsonl',run_id)
        window=NativeWindow(app,native,profile);window.start()
        report['components']['native']['status']='running'
        # The power assertion belongs to this synthetic load only. Never mask
        # the service's own idle-sleep behavior in the physical acceptance.
        awake=subprocess.Popen(['/usr/bin/caffeinate','-dimsu','-w',str(config.pids['owned_node_tree'])],stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        warm_ids=[]
        for index in range(120):
            item=sweep(warm,index);warm_ids.append(item['id'])
            client.json(client.session_path(warm)+'/sweeps/batch',method='POST',payload={'items':[item]},status=201)
            if (native/'native-ready.json').is_file():break
            window_state=window.owner.status()
            if window_state and window_state.get('phase') in ('failed','exited','released','quiescent'):
                raise BenchmarkError('native_unavailable','Native process exited during warmup')
            time.sleep(.5)
        wait_json(native/'native-ready.json',lambda row:row.get('run_id')==run_id and row.get('source_id') in warm_ids,owner=window)
        background=RCHistoryAndBackup(config,node,config.duration_seconds/2)
        queue=QueueProbe(config,events.emit)
        def ready():
            publish_stop(native/'use-workload.request',run_id)
            wait_json(native/'native-stream-ready.json',lambda row:row.get('run_id')==run_id and row.get('session_id')==config.session_id,owner=window)
            events.emit('load.begin');queue.start();background.start()
        produced,producer_status=run(config,before_production=ready)
        report.update(producer_status=producer_status,producer_report='producer/report.json',counts=produced.get('counts'),
                      production_elapsed_seconds=produced.get('production',{}).get('production_elapsed_seconds',0))
        report['components']['production']['status']='passed' if producer_status==0 else 'failed'
        background.production_done.set();background.wait();background.cancel();background.join()
        queue.thread.join(config.request_timeout+2)
        if queue.thread.is_alive():queue.close()
        report.update(producer_status=producer_status,producer_report='producer/report.json',counts=produced.get('counts'),
                      background=background.report,queue_errors=queue.errors,warmup_sources=warm_ids)
        window.stop(run_id);window=None
        report['components']['final_backup']['status']='running'
        node.request_final_backup()
        report['final_backup']=node.wait_for_backup(final=True)
        report['components']['final_backup']['status']='passed' if report['final_backup']['phase']=='completed' else 'failed'
        if report['final_backup']['phase']!='completed':raise BenchmarkError('backup_failed','Final complete backup not committed')
        node.close()
        restore_dir=root/'restore-verification';restore_dir.mkdir(mode=0o700)
        report['components']['restore']['status']='running'
        restored=PackageNode(node.binary,restore_dir,run_id+'-restore',restore_bundle=node.final_bundle)
        try:
            restored_config=replace(config,base_url=restored.start(),output_dir=root/'unused-restore')
            # A fresh ledger uses the original proposed generator, never just counts.
            from .. import verify
            ledger=restored_ledger(root/'producer/offers.sqlite',root/'restore-offers.sqlite')
            try:
                restore_proof=verify.archive(Client(restored_config),restored_config,ledger)
            finally:ledger.close()
            restored.close();report['restore']=restore_proof
            report['components']['restore']['status']=restore_proof['status']
        finally:restored.close(expect_success=False)
        events.emit('load.completed');events.close();events=None
    except (Exception,KeyboardInterrupt) as error:
        report['errors'].append(dict(code=getattr(error,'code','interrupted' if isinstance(error,KeyboardInterrupt) else 'load_failed'),message=str(error)))
    finally:
        if queue is not None:
            try:queue.close()
            except Exception as error:report['errors'].append(dict(code='queue_cleanup',message=str(error)))
        if background is not None:
            background.production_done.set();background.cancel()
            if background.thread.ident is not None:
                try:background.join()
                except Exception as error:report['errors'].append(dict(code='background_cleanup',message=str(error)))
            report['background']=background.report
            report['components']['online_backup']['status']=background.report.get('status','failed')
        if window is not None:
            try:window.stop(report.get('run_id',config.run_id),required=False)
            except Exception as error:report['errors'].append(dict(code='native_cleanup',message=str(error)))
        if node is not None:
            try:node.close(expect_success=False)
            except Exception as error:report['errors'].append(dict(code='core_cleanup',message=str(error)))
            report['owned_nodes']=node.records
        if awake is not None:
            if awake.poll() is None:awake.terminate()
            try:awake.wait(timeout=5)
            except subprocess.TimeoutExpired:awake.kill();awake.wait(timeout=5)
        if events is not None:
            try:events.close()
            except Exception as error:report['errors'].append(dict(code='controller_cleanup',message=str(error)))
        if config is not None and node is not None and native is not None:
            try:
                report['native']=closed_native_evidence(config,node,native,root/'controller.jsonl',warm_ids,identity)
                report['components']['native']['status']=report['native']['status']
            except Exception as error:
                report['native']={'status':'failed','errors':[str(error)]}
                report['components']['native']['status']='failed'
                report['errors'].append(dict(code='native_evidence',message=str(error)))
        if identity is not None:
            try:
                if bundle_identity(app,manifest)!=identity:raise BenchmarkError('candidate_changed','App changed during load')
                report['candidate_verified_after_run']=True
            except Exception as error:
                report['errors'].append(dict(code=getattr(error,'code','candidate_changed'),message=str(error)))
        for component in report['components'].values():
            if component['status']=='running':component['status']='failed'
        cleanup_errors=[error for error in report['errors'] if 'cleanup' in error['code']]
        report['components']['cleanup']={'status':'failed' if cleanup_errors else 'passed',
            'owned_processes_joined':all(row.get('joined') is True for row in report.get('owned_nodes',[])),
            'runtime_exit_codes':[row.get('exit_code') for row in report.get('owned_nodes',[])]}
        background_result=report.get('background',{})
        report['software_concurrency_observed']=background_result.get('backup_running_during_history') is True and background_result.get('history_during_production') is True
        report['successful']=bool(config is not None and not report['errors'] and report.get('producer_status')==0
            and not report.get('queue_errors') and background_result.get('status')=='passed'
            and complete_restore(config,report.get('restore',{})) and report.get('native',{}).get('status')=='passed'
            and report['software_concurrency_observed'] and report['components']['cleanup']['status']=='passed')
        report['point2_passed']=report['successful'] and smoke_seconds is None and report.get('production_elapsed_seconds',0)>=1800
        status=0 if report['successful'] else 1
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        atomic_report(root,report)
    return report,status
