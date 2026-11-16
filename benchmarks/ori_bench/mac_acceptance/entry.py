#!/usr/bin/env python3
"""Offline engineering launcher. Neither a smoke nor a build closes acceptance."""
from pathlib import Path
import sys
PAYLOAD=Path(__file__).resolve().parents[3]
if str(PAYLOAD) not in sys.path:sys.path.insert(0,str(PAYLOAD))

import argparse
from dataclasses import replace
from datetime import datetime,timezone
import json
import os
import subprocess
import time
import uuid
from benchmarks.ori_bench.config import Config,BenchmarkError
from benchmarks.ori_bench.client import Client
from benchmarks.ori_bench.endurance_node import ROOT
from benchmarks.ori_bench.runner import atomic_report,output_directory
from benchmarks.ori_bench.source import session,sweep,observation
from benchmarks.ori_bench.mac_acceptance.evidence import bundle_identity
from benchmarks.ori_bench.mac_acceptance.node import PackageNode
from benchmarks.ori_bench.mac_acceptance.sources import SourceProof,export
from tools.release import macos_rc_package as package


def self_check(app,manifest,output):
    root=output_directory(output).resolve();report=dict(successful=False,smoke_only=True,
        point2_passed=False,point4_passed=False,hardware_tested=False,errors=[])
    node=restored=None
    try:
        report['identity']=bundle_identity(app,manifest)
        from tests.release_process_ownership import OwnedCommand
        registry=root/'keeper-registry';registry.mkdir(mode=0o700)
        with (root/'keeper.log').open('xb') as log:
            owner=OwnedCommand.start([sys.executable,'-I','-S','-c','import ssl,sqlite3,ctypes,sys; print(sys.executable)'],
                environment=os.environ.copy(),registry=registry,root=ROOT,log=log,shutdown_timeout=15)
            if owner.wait(15)!=0:raise BenchmarkError('runtime_failed','Isolated owned Python child failed')
            owner.release(15)
        binary=Path(app)/'Contents/Helpers/ori-core';out=root/'node';out.mkdir(mode=0o700)
        run_id='kit-smoke-'+uuid.uuid4().hex;node=PackageNode(binary,out,run_id)
        config=Config(node.start(),root/'unused',run_id=run_id,workload_profile='macos-rc1',points=4,duration_seconds=.1)
        client=Client(config);client.json('/api/v1/sessions',method='POST',payload=session(config),status=201)
        for kind,make in [('sweeps',sweep),('observations',observation)]:
            client.json(client.session_path(config)+'/'+kind+'/batch',method='POST',payload={'items':[make(config,0)]},status=201)
        from benchmarks.ori_bench.mac_acceptance.queue_probe import QueueProbe
        probe=QueueProbe(config,lambda *args:None);probe.start();probe.thread.join(60);probe.close()
        if probe.errors:raise BenchmarkError('queue_failed',str(probe.errors))
        proof=SourceProof(root/'originals.sqlite')
        try:
            export(client,config.session_id,proof.original)
            node.request_backup();deadline=time.monotonic()+30
            while node.backup_state()['phase'] not in ('completed','failed') and time.monotonic()<deadline:time.sleep(.05)
            if node.backup_state()['phase']!='completed':raise BenchmarkError('backup_failed','Actual packaged owner backup incomplete')
            node.close();restore=root/'restore';restore.mkdir(mode=0o700)
            restored=PackageNode(binary,restore,run_id+'-restore',restore_bundle=node.bundle)
            rc=replace(config,base_url=restored.start());export(Client(rc),config.session_id,proof.restored)
            report['source_proof']=proof.finish()
        finally:proof.close()
        restored.close();report['successful']=report['source_proof']['status']=='passed'
    except Exception as error:report['errors'].append(dict(code=getattr(error,'code','self_check_failed'),message=str(error)))
    finally:
        for owned in (restored,node):
            if owned:
                try:owned.close(expect_success=False)
                except Exception as error:report['errors'].append(dict(code='cleanup_failed',message=str(error)))
        if report['errors']:report['successful']=False
        atomic_report(root,report)
    return report,0 if report['successful'] else 1


def verify_kit(root):
    manifest=package.read_json(root/'kit-manifest.json',32*1024*1024)
    if [entry for entry in package.inventory(root) if entry['path']!='kit-manifest.json']!=manifest['files']:
        raise ValueError('Engineering kit differs from its inventory')
    return manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app',type=Path,default=Path('/Applications/OpenRadioInspector RC.app'))
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--phase',choices=['load','hardware','all'])
    parser.add_argument('--load-report',type=Path)
    parser.add_argument('--self-check',action='store_true')
    parser.add_argument('--kit-root',type=Path)
    args=parser.parse_args(argv)
    os.umask(0o077)
    if args.kit_root:verify_kit(args.kit_root.resolve(strict=True))
    manifest=args.manifest or (args.kit_root/'app-manifest.json' if args.kit_root else None)
    if manifest is None:parser.error('--manifest or --kit-root is required')
    if args.self_check:
        if args.output_dir is None:parser.error('Self-check needs a new --output-dir')
        report,status=self_check(args.app,manifest,args.output_dir)
    else:
        phase=args.phase
        if phase is None:
            print('1 — полная нагрузка (30 минут)\n2 — приборы (8 часов; нужен успешный отчёт нагрузки)\n3 — оба теста последовательно')
            phase={'1':'load','2':'hardware','3':'all'}.get(input('Номер: ').strip())
            if phase is None:parser.error('Select 1, 2 or 3')
        root=args.output_dir or Path.home()/'Documents/OpenRadioInspector-Tests'/(
            datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
        root=output_directory(root).resolve(strict=True)
        from benchmarks.ori_bench.mac_acceptance.load import run_load
        from benchmarks.ori_bench.mac_acceptance.hardware import run_hardware
        if phase in ('load','all'):
            print('Оставьте окно графика видимым и неподвижным. Разрешите тестовую запись экрана: сохраняется только время показа маркера, без снимков.',flush=True)
            report,status=run_load(args.app,manifest,root/'load')
            if phase=='all' and status==0 and report['point2_passed']:
                report,status=run_hardware(args.app,manifest,root/'hardware',root/'load/report.json')
        else:
            previous=args.load_report or Path(input('Полный путь к успешному load/report.json: ').strip()).expanduser()
            report,status=run_hardware(args.app,manifest,root/'hardware',previous)
        atomic_report(root,dict(successful=status==0,phase=phase,point2_passed=(root/'load/report.json').is_file() and json.loads((root/'load/report.json').read_bytes()).get('point2_passed') is True,
                                point4_passed=report.get('point4_passed',False),hardware_tested=report.get('hardware_tested',False)))
        print('Отчёты и исходники: '+str(root),flush=True)
    print(json.dumps(dict(successful=report['successful'],point2_passed=report.get('point2_passed',False),
                          point4_passed=report.get('point4_passed',False),hardware_tested=report.get('hardware_tested',False))))
    return status


if __name__=='__main__':
    try:raise SystemExit(main())
    except (BenchmarkError,ValueError,OSError,KeyboardInterrupt) as error:
        print('Проверка не завершена: '+str(error),file=sys.stderr);raise SystemExit(1)
