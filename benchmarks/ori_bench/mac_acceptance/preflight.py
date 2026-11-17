"""Read-only host prerequisites. Readiness never constitutes load/hardware acceptance."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import socket
import subprocess
from datetime import datetime,timezone

from ..config import Config
from ..macos_rc import host
from ..runner import atomic_report,output_directory
from ..source import sweep,observation,encode
from .evidence import bundle_identity,verify_host
from tools.modernization.api_parity import load_json
from tools.release import macos_rc_package as package


def native_host_info(app,root,identity):
    build=package.read_json(Path(app)/package.BUILD_INFO,65536)
    if build.get('acceptance_protocol')!=2:
        raise ValueError('Candidate lacks acceptance protocol2; native CLI was not started')
    result=subprocess.run([str(Path(app)/'Contents/MacOS/OpenRadioInspectorRC'),'--acceptance-host-info',str(root)],capture_output=True,timeout=30)
    if result.returncode!=0:raise RuntimeError('Native host probe failed; no service was started')
    native=load_json(result.stdout,65536)
    if not isinstance(native,dict) or native.get('native_sha256')!=identity['native_sha256']:
        raise RuntimeError('Native probe identity mismatch')
    return native


def validate_local_output(app,root,identity):
    root=Path(root).resolve(strict=True)
    if root.stat().st_uid!=os.geteuid() or root.stat().st_mode&0o077 \
            or any(part in ('CloudStorage','Mobile Documents','FileProvider') for part in root.parts):
        raise ValueError('Use a private local directory outside cloud/FileProvider storage')
    native=native_host_info(app,root,identity)
    if native.get('volume_is_local') is not True or native.get('volume_is_read_only') is not False:
        raise ValueError('The live test archive requires a writable local volume')
    return native


def assess_preflight(facts):
    native=facts.get('native') or {}
    def integer(value):return type(value) is int and value>=0
    free,required=facts.get('free_bytes'),facts.get('required_bytes')
    checks={
        'target_host':verify_host(facts.get('host',{})),
        'signature':facts.get('signature_verified') is True,
        'inventory':facts.get('inventory_verified') is True,
        'private_local_directory':facts.get('private_directory') is True and facts.get('cloud_directory') is False and facts.get('local_volume') is True,
        'storage_budget':integer(free) and integer(required) and required>0 and free>=required,
        'power':facts.get('power_source')=='AC Power',
        'screen_capture':native.get('screen_capture_allowed') is True,
        'unlocked_console':native.get('screen_locked') is False,
        'geometry':type(native.get('screen_width')) in (int,float) and native['screen_width']>=1200
                   and type(native.get('screen_height')) in (int,float) and native['screen_height']>=768,
        'test_port':facts.get('test_port_available') is True,
        'service_scope_idle':facts.get('acceptance_pointer_present') is False,
        'device_owners':type(facts.get('device_owners')) is list and not facts['device_owners'],
    }
    return dict(ready=all(checks.values()),checks=checks,point2_passed=False,point4_passed=False)


def storage_requirement():
    config=Config('http://127.0.0.1:1',Path('/unused'),run_id='macload-'+'0'*32,workload_profile='macos-rc1',duration_seconds=1800)
    counts=config.planned_counts()
    # Two retained full load runs, active+backup+restore/ledgers/logs allowance.
    sizes={kind:max(len(encode(make(config,i))) for i in (0,counts[kind]//2,counts[kind]-1))
           for kind,make in [('sweep',sweep),('observation',observation)]}
    source_bytes=sum(sizes[k]*counts[k] for k in sizes)
    history=Config('http://127.0.0.1:1',Path('/unused-history'),mode='history',points=290,allow_million=True,
                   workload_profile='macos-rc1',run_id='machistory-'+'0'*32)
    history_bytes=max(len(encode(sweep(history,i))) for i in (0,500_000,999_999))*1_000_000
    return 2*(8*source_bytes+512*1024**2)+1024**3+3*history_bytes+256*1024**2


def run_preflight(app,manifest,output,*,device_ports=()):
    root=output_directory(output).resolve(strict=True)
    facts=dict(host=host(),signature_verified=False,inventory_verified=False,
               private_directory=hasattr(os,'geteuid') and root.stat().st_uid==os.geteuid() and root.stat().st_mode&0o077==0,
               cloud_directory=any(part in ('CloudStorage','Mobile Documents','FileProvider') for part in root.parts),
               local_volume=False,free_bytes=shutil.disk_usage(root).free,required_bytes=storage_requirement(),
               native={},test_port_available=False,acceptance_pointer_present=(Path.home()/'Library/Application Support/OpenRadioInspector/macOS-Acceptance/active.json').exists(),
               device_owners=[],power_source=None)
    errors=[];identity=None
    try:
        identity=bundle_identity(app,manifest);facts['inventory_verified']=True
        if facts['host']['system']=='Darwin':
            result=subprocess.run(['/usr/bin/codesign','--verify','--strict','--deep',str(app)],capture_output=True,timeout=60)
            facts['signature_verified']=result.returncode==0
            native=native_host_info(app,root,identity)
            facts['native']=native;facts['local_volume']=native.get('volume_is_local') is True
            power=subprocess.check_output(['/usr/bin/pmset','-g','batt'],text=True,timeout=5).splitlines()[0]
            facts['power_source']='AC Power' if "'AC Power'" in power else 'Battery Power'
            for port in device_ports:
                # lsof observes owners; the probe never opens a serial device.
                result=subprocess.run(['/usr/sbin/lsof','-Fpc','--',str(port)],capture_output=True,text=True,timeout=5)
                if result.returncode not in (0,1):raise RuntimeError('Unable to establish serial-port ownership')
                if result.stdout.strip():facts['device_owners'].append(dict(port=str(port),owners=result.stdout.splitlines()))
            with socket.socket() as probe:
                try:probe.bind(('127.0.0.1',8774));facts['test_port_available']=True
                except OSError:pass
    except Exception as error:errors.append(str(error))
    report=dict(schema_version=1,profile='macos-preflight',smoke_only=True,hardware_tested=False,
        started_at=datetime.now(timezone.utc).isoformat(),identity=identity,facts=facts,errors=errors,
        hardware_storage='Must be measured and admitted by live preparation before arming; no invented RF rate',
        **assess_preflight(facts))
    report['successful']=report['ready'] and not errors
    atomic_report(root,report)
    return report,0 if report['successful'] else 1
