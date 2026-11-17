"""Packaged HTTP rejection checks and real ENOSPC on an owned 256-MiB image."""
from dataclasses import replace
import errno
import http.client
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import uuid
from urllib.parse import urlsplit

from ..client import Client,HTTPFailure
from ..config import Config,BenchmarkError
from ..macos_rc import host
from ..runner import output_directory,atomic_report
from ..source import session,sweep,encode,measurement_hash
from .evidence import bundle_identity,verify_host
from .node import PackageNode


def validate_fault_mount(image,mount,info,*,host_device,mount_device):
    image=Path(image).resolve(strict=True);mount=Path(mount).resolve(strict=True)
    if mount==Path('/') or mount_device==host_device:
        raise BenchmarkError('unsafe_fault_volume','Refusing to fill the host filesystem')
    for item in info.get('images',[]):
        if Path(item.get('image-path','')).resolve()!=image:continue
        for entry in item.get('system-entities',[]):
            if entry.get('mount-point')==str(mount) and str(entry.get('dev-entry','')).startswith('/dev/disk'):
                return entry['dev-entry']
    raise BenchmarkError('unsafe_fault_volume','Mount is not backed by this run’s bounded image')


def raw_request(base,path,body,*,headers=(),length=None,truncated=False,closed=False):
    origin=urlsplit(base)
    if origin.scheme!='http' or origin.hostname!='127.0.0.1':raise BenchmarkError('unsafe_target','Owned loopback target required')
    fields=[('Host',origin.netloc),('Content-Type','application/json'),*headers]
    if length is not None:fields.append(('Content-Length',str(length)))
    data=('POST '+path+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in fields)+'\r\n').encode('ascii')
    with socket.create_connection(('127.0.0.1',origin.port),timeout=13) as peer:
        peer.sendall(data+body)
        if truncated:peer.shutdown(socket.SHUT_WR)
        response=http.client.HTTPResponse(peer);response.begin();response.read(1024*1024)
        if closed:
            if not response.will_close:raise BenchmarkError('framing_connection_open','Rejected framing did not close its connection')
            try:remaining=peer.recv(1)
            except ConnectionResetError:remaining=b''
            if remaining:raise BenchmarkError('framing_connection_open','Unexpected bytes after rejected request')
        return response.status


def http_faults(client,config):
    client.json('/api/v1/sessions',method='POST',payload=session(config),status=201)
    path=client.session_path(config)+'/sweeps/batch';original=sweep(config,0)
    limit=16*1024**2;body=encode({'items':[original]})
    if len(body)>limit:raise BenchmarkError('invalid_fixture','Boundary source is oversized')
    status=raw_request(config.base_url,path,body+b' '*(limit-len(body)),length=limit)
    if status!=201:raise BenchmarkError('boundary_rejected','Exactly16MiB valid measurement request was refused: '+str(status))
    cases=[('one_byte_over',b'',limit+1,(),False,True,413),
        ('huge_length',b'',2**64-1,(),False,True,413),
        ('duplicate_length',b'',0,(('Content-Length','0'),),False,True,400),
        ('transfer_encoding',b'0\r\n\r\n',None,(('Transfer-Encoding','chunked'),),False,True,400),
        ('truncated',b'{',5,(),True,True,400),('body_deadline',b'',1,(),False,True,408),
        ('duplicate_json',b'{"items":[],"items":[]}',None,(),False,False,400),
        ('nonfinite_json',b'{"items":[NaN]}',None,(),False,False,400),
        ('invalid_utf8',b'\xff',None,(),False,False,400)]
    rejections=[]
    for name,payload,length,headers,truncated,closed,expected in cases:
        if length is None and name!='transfer_encoding':length=len(payload)
        actual=raw_request(config.base_url,path,payload,length=length,headers=headers,truncated=truncated,closed=closed)
        rejections.append(dict(case=name,status=actual,expected=expected))
        if actual!=expected:raise BenchmarkError('rejection_failed',name+' returned '+str(actual)+' instead of '+str(expected))
        rows=client.json(client.session_path(config)+'/sweeps?limit=100')
        if len(rows)!=1 or measurement_hash('sweep',rows[0])!=measurement_hash('sweep',original):
            raise BenchmarkError('rejected_write','Rejected request changed archived sources')
    return dict(status='passed',rejections=rejections,source_count=1,original_id=original['id'],exact_boundary_bytes=limit)


def disk_fault(binary,root):
    image=root/'bounded-256MiB.sparseimage';mount=root/'mount';mount.mkdir(mode=0o700)
    log=[];mounted=False;node=None;filler=None
    def command(*args):
        result=subprocess.run(args,capture_output=True,timeout=90)
        log.append(dict(command=Path(args[0]).name,operation=args[1],exit_code=result.returncode,
                        stderr=result.stderr.decode('utf-8',errors='replace')[-4096:]))
        if result.returncode:raise BenchmarkError('fault_volume_failed','Owned hdiutil operation failed')
        return result.stdout
    try:
        if shutil.disk_usage(root).free<1024**3:raise BenchmarkError('insufficient_space','At least1GiB for a bounded256MiB test image is required')
        command('/usr/bin/hdiutil','create','-size','256m','-fs','APFS','-type','SPARSE','-volname','ORI bounded ENOSPC',str(image))
        command('/usr/bin/hdiutil','attach','-nobrowse','-mountpoint',str(mount),str(image));mounted=True
        info=plistlib.loads(command('/usr/bin/hdiutil','info','-plist'))
        device=validate_fault_mount(image,mount,info,host_device=os.stat(root).st_dev,mount_device=os.stat(mount).st_dev)
        data=mount/'data';data.mkdir(mode=0o700)
        class BoundedNode(PackageNode):
            def _adapter_command(self,_data):return super()._adapter_command(data)
        out=root/'node';out.mkdir(mode=0o700);run_id='macdisk-'+uuid.uuid4().hex
        node=BoundedNode(binary,out,run_id)
        config=Config(node.start(),root/'unused',run_id=run_id,points=4,workload_profile='macos-rc1');client=Client(config)
        client.json('/api/v1/sessions',method='POST',payload=session(config),status=201)
        first=sweep(config,0)
        client.json(client.session_path(config)+'/sweeps/batch',method='POST',payload={'items':[first]},status=201)
        filler=mount/'generated-fill.bin';filled=0
        descriptor=os.open(filler,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'wb',buffering=0) as target:
            for chunk in (1024*1024,4096):
                while filled<512*1024**2:
                    try:filled+=target.write(b'x'*chunk)
                    except OSError as error:
                        if error.errno not in (errno.ENOSPC,errno.EDQUOT):raise
                        break
            if filled>=512*1024**2:raise BenchmarkError('unsafe_fault_volume','Bounded image did not reach ENOSPC; no host disk was filled')
        offered=sweep(replace(config,points=4096),1);failure=None
        try:client.json(client.session_path(config)+'/sweeps/batch',method='POST',payload={'items':[offered]},status=201)
        except HTTPFailure as error:failure=error.status
        if failure is None or failure<500:raise BenchmarkError('disk_fault_not_observed','Full image did not visibly reject the archive write')
        # Only this generated filler is removed; every database/source stays in
        # the retained image. The startup volume and its measurements are untouched.
        filler.unlink();filler=None
        rows=client.json(client.session_path(config)+'/sweeps?limit=100')
        if len(rows)!=1 or measurement_hash('sweep',rows[0])!=measurement_hash('sweep',first):
            raise BenchmarkError('disk_fault_corrupted_source','ENOSPC changed or partly committed an original')
        next_source=sweep(config,2)
        client.json(client.session_path(config)+'/sweeps/batch',method='POST',payload={'items':[next_source]},status=201)
        rows=client.json(client.session_path(config)+'/sweeps?limit=100')
        if [measurement_hash('sweep',s) for s in rows]!=[measurement_hash('sweep',s) for s in (first,next_source)]:
            raise BenchmarkError('disk_recovery_failed','Archive did not recover with its original sources')
        node.close()
        return dict(status='passed',device=device,image=str(image),filled_bytes=filled,rejected_status=failure,
                    preserved_ids=[first['id'],next_source['id']],commands=log)
    finally:
        if filler is not None and filler.exists():filler.unlink()
        if node:node.close(expect_success=False)
        if mounted:command('/usr/bin/hdiutil','detach',str(mount))


def run_faults(app,manifest,output):
    root=output_directory(output).resolve(strict=True)
    report=dict(profile='macos-bounded-faults',successful=False,point2_passed=False,point4_passed=False,
                hardware_tested=False,errors=[],host=host(),components={})
    node=None
    try:
        if not verify_host(report['host']):raise BenchmarkError('wrong_host','Bounded macOS fault acceptance requires the actual target Mac')
        identity=bundle_identity(app,manifest);report['identity']=identity
        binary=Path(app)/'Contents/Helpers/ori-core';out=root/'http-node';out.mkdir(mode=0o700)
        run_id='macfault-'+uuid.uuid4().hex;node=PackageNode(binary,out,run_id)
        config=Config(node.start(),root/'unused',run_id=run_id,points=4,workload_profile='macos-rc1')
        report['components']['http']=http_faults(Client(config),config);node.close()
        disk=root/'disk';disk.mkdir(mode=0o700)
        report['components']['disk']=disk_fault(binary,disk)
        if bundle_identity(app,manifest)!=identity:raise BenchmarkError('candidate_changed','App changed during fault checks')
        report['successful']=True
    except (Exception,KeyboardInterrupt) as error:
        report['errors'].append(dict(code=getattr(error,'code','fault_failed'),message=str(error)))
    finally:
        if node:
            try:node.close(expect_success=False)
            except Exception as error:report['errors'].append(dict(code='cleanup_failed',message=str(error)))
        report['successful']=report['successful'] and not report['errors'];atomic_report(root,report)
    return report,0 if report['successful'] else 1
