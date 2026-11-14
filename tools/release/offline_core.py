#!/usr/bin/env python3
"""Offline assembly, relocation verification and owned Linux release rehearsal.

Consumes prepared local payloads only. No downloads, package installation,
system service registration, automatic backend selection or existing data writes.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import stat
import struct
import sys
import tempfile
import time
import zipfile

try:
    from . import _offline_process as child
except ImportError:
    import _offline_process as child

CHUNK=1024*1024
META={'manifest.json','SHA256SUMS'}
ARCHES={'x86_64':62,'aarch64':183}
SYSTEM_ABI={'libc.so.6','libm.so.6','libpthread.so.0','libdl.so.2','librt.so.1',
            'libutil.so.1','libgcc_s.so.1','libstdc++.so.6',
            'ld-linux-x86-64.so.2','ld-linux-aarch64.so.1'}
REQUIRED_PACKAGES={'legacy':{'openradioinspector','numpy','pyserial'},
                   'research':{'openradioinspector','ori-research','numpy','duckdb','pyarrow'}}
CORE_MAIN='from radioinspector.launcher import core_main; raise SystemExit(core_main())'
PRIVATE_NAMES={'.git','.ssh','.aws','.ori-runtime','.env','.netrc','.npmrc',
               'credentials.json','tokens.json','tasks.json','core.lock'}


class ReleaseError(RuntimeError):
    """Stable public code only; never carry raw child output or input contents."""


@dataclass(frozen=True)
class Limits:
    max_entries:int=100000
    max_file_bytes:int=2*1024**3
    max_total_bytes:int=8*1024**3
    max_manifest_bytes:int=64*1024**2

    def validate(self):
        for name,value in vars(self).items():
            if type(value) is not int or not 0<value<=getattr(Limits(),name):raise ReleaseError('invalid_limits')


def canonical(value):
    return (json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':'))+'\n').encode()


def strict_json(data):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result:raise ReleaseError('duplicate_json_key')
            result[k]=v
        return result
    def number(value):
        import math
        result=float(value)
        if not math.isfinite(result):raise ReleaseError('nonfinite_json')
        return result
    try:return json.loads(data,object_pairs_hook=pairs,parse_float=number,
                          parse_constant=lambda _:(_ for _ in ()).throw(ReleaseError('nonfinite_json')))
    except (ValueError,UnicodeError,RecursionError) as error:raise ReleaseError('invalid_json') from error


def safe_path(value):
    if not isinstance(value,str) or not value:
        raise ReleaseError('unsafe_path')
    try:size=len(value.encode('utf-8'))
    except UnicodeError as error:raise ReleaseError('unsafe_path') from error
    if size>4096:
        raise ReleaseError('unsafe_path')
    if any(ord(c)<32 or ord(c)==127 for c in value) or '\\' in value or ':' in value:
        raise ReleaseError('unsafe_path')
    path=PurePosixPath(value)
    if path.is_absolute() or path.as_posix()!=value or any(p in ('','.','..') for p in path.parts):raise ReleaseError('unsafe_path')
    return path


def private_path(value):
    path=safe_path(value)
    if any(p in PRIVATE_NAMES for p in path.parts) or '.sqlite' in path.name or path.name.endswith(('.db','.key','.p12')):
        raise ReleaseError('private_state_in_payload')


def _certificate_chunk(data,tail,first):
    if first and b'-----BEGIN CERTIFICATE-----' not in data and b'-----BEGIN TRUSTED CERTIFICATE-----' not in data:
        raise ReleaseError('nonpublic_pem_payload')
    combined=tail+data
    if b'PRIVATE KEY' in combined:raise ReleaseError('private_key_in_payload')
    return combined[-32:]


def link_destination(path,target):
    if not isinstance(target,str) or not target or target.startswith('/') or '\\' in target or ':' in target or any(ord(c)<32 for c in target):
        raise ReleaseError('unsafe_symlink')
    parts=list(PurePosixPath(path).parent.parts)
    for part in target.split('/'):
        if part in ('','.'):continue
        if part=='..':
            if not parts:raise ReleaseError('escaping_symlink')
            parts.pop()
        else:parts.append(part)
    if not parts:raise ReleaseError('unsafe_symlink')
    return '/'.join(parts)


def check_links(entries):
    by_path={e['path']:e for e in entries}
    for entry in entries:
        if entry['kind']!='symlink':continue
        path=entry['path'];seen=set()
        for _ in range(64):
            if path in seen:raise ReleaseError('cyclic_symlink')
            seen.add(path);item=by_path.get(path)
            if item is None:raise ReleaseError('broken_symlink')
            if item['kind']!='symlink':break
            path=link_destination(path,item['target'])
        else:raise ReleaseError('symlink_depth_limit')


def _scan(root,limits,*,hash_files=True,allow_hardlinks=False,skip_metadata=True):
    limits.validate();root=Path(root).absolute();entries=[];total=0;inventory_budget=0
    if root.is_symlink() or not root.is_dir():raise ReleaseError('unsafe_payload_root')
    def walk(fd,prefix='',depth=0):
        nonlocal total,inventory_budget
        if depth>64:raise ReleaseError('path_depth_limit')
        names=[]
        with os.scandir(fd) as iterator:
            for item in iterator:
                relative=f'{prefix}/{item.name}' if prefix else item.name
                if skip_metadata and relative in META:continue
                if len(names)+len(entries)>=limits.max_entries:raise ReleaseError('inventory_size_limit')
                names.append(item.name)
        names.sort(key=os.fsencode)
        for name in names:
            relative=f'{prefix}/{name}' if prefix else name
            if skip_metadata and relative in META:continue
            private_path(relative)
            inventory_budget+=len(relative.encode('utf-8'))*2+512
            if inventory_budget>limits.max_manifest_bytes:raise ReleaseError('manifest_size_limit')
            info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            mode=stat.S_IMODE(info.st_mode)
            if mode&0o7000:raise ReleaseError('special_permissions')
            if stat.S_ISDIR(info.st_mode):kind='directory';size=0
            elif stat.S_ISREG(info.st_mode):
                kind='file';size=info.st_size
                if not allow_hardlinks and info.st_nlink!=1:raise ReleaseError('hardlinked_payload')
                if size>limits.max_file_bytes:raise ReleaseError('file_size_limit')
            elif stat.S_ISLNK(info.st_mode):kind='symlink';size=info.st_size
            else:raise ReleaseError('special_file')
            total+=size
            if len(entries)>=limits.max_entries or total>limits.max_total_bytes:raise ReleaseError('inventory_size_limit')
            entry={'path':relative,'kind':kind,'mode':mode,'size':size}
            if kind=='symlink':
                target=os.readlink(name,dir_fd=fd);link_destination(relative,target);entry['target']=target
            elif kind=='file' and hash_files:
                opened=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    before=os.fstat(opened)
                    if not stat.S_ISREG(before.st_mode) or (before.st_dev,before.st_ino,before.st_size)!=(info.st_dev,info.st_ino,size):raise ReleaseError('payload_changed')
                    digest=hashlib.sha256();count=0;tail=b''
                    certificate=Path(name).suffix.lower() in ('.pem','.crt')
                    while chunk:=os.read(opened,CHUNK):
                        if certificate:tail=_certificate_chunk(chunk,tail,count==0)
                        count+=len(chunk)
                        if count>size:raise ReleaseError('payload_changed')
                        digest.update(chunk)
                    after=os.fstat(opened)
                    if count!=size or (before.st_mtime_ns,before.st_size)!=(after.st_mtime_ns,after.st_size):raise ReleaseError('payload_changed')
                    entry['sha256']=digest.hexdigest()
                finally:os.close(opened)
            entries.append(entry)
            if kind=='directory':
                nested=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:walk(nested,relative,depth+1)
                finally:os.close(nested)
    fd=_open_directory(root)
    try:walk(fd)
    finally:os.close(fd)
    entries.sort(key=lambda e:e['path'].encode('utf-8'))
    check_links(entries)
    return entries


def inventory(root,limits=Limits()):
    try:return _scan(root,limits)
    except (OSError,UnicodeError) as error:raise ReleaseError('payload_io_error') from error


def validate_manifest(value,limits=Limits()):
    limits.validate()
    keys={'format','manifest_version','release_version','source_revision','architecture','components','runtimes','wheelhouse','inventory'}
    if not isinstance(value,dict) or set(value)!=keys or value['format']!='ori-offline-core-release' or value['manifest_version']!=1:raise ReleaseError('manifest_schema')
    _identity(value['release_version'],value['source_revision'],value['architecture'])
    if type(value['manifest_version']) is not int or value['wheelhouse'] not in ('not_supplied','optional_repair_artifacts'):raise ReleaseError('manifest_schema')
    components=value['components'];runtimes=value['runtimes']
    if not isinstance(components,dict) or set(components)!={'ori_core','ori_supervisor'} or not isinstance(runtimes,dict) or set(runtimes)!={'legacy','research'}:raise ReleaseError('manifest_schema')
    for item in components.values():
        if not isinstance(item,dict) or not {'version','sha256'}<=set(item) or set(item)-{'version','sha256','build_revision'}:raise ReleaseError('component_spec')
        if not isinstance(item['version'],str) or not re.fullmatch('[0-9][A-Za-z0-9.+_-]{0,63}',item['version']) or not isinstance(item['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',item['sha256']):raise ReleaseError('component_spec')
        revision=item.get('build_revision')
        if revision is not None and (not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{40}',revision)):raise ReleaseError('component_build_revision')
    for role,item in runtimes.items():
        if not isinstance(item,dict) or set(item)!={'python_version','packages'} or not isinstance(item['python_version'],str) or not re.fullmatch(r'3\.[0-9]+\.[0-9]+',item['python_version']):raise ReleaseError('runtime_spec')
        packages=item['packages']
        if not isinstance(packages,dict) or not REQUIRED_PACKAGES[role]<=set(packages) or len(packages)>64:raise ReleaseError('missing_runtime_dependency')
        if any(not re.fullmatch('[A-Za-z0-9_.+-]{1,80}',k) or not isinstance(v,str) or not re.fullmatch('[A-Za-z0-9_.+-]{1,80}',v) for k,v in packages.items()):raise ReleaseError('runtime_package_version')
        if role=='research' and not item['python_version'].startswith('3.15.'):raise ReleaseError('runtime_version')
    entries=value['inventory']
    if not isinstance(entries,list) or len(entries)>limits.max_entries:raise ReleaseError('inventory_size_limit')
    seen=set();total=0
    for e in entries:
        if not isinstance(e,dict) or not {'path','kind','mode','size'}<=set(e):raise ReleaseError('inventory_schema')
        private_path(e['path'])
        if e['path'] in META or e['path'] in seen:raise ReleaseError('duplicate_inventory_path')
        seen.add(e['path'])
        if type(e['size']) is not int or e['size']<0 or e['size']>limits.max_file_bytes:raise ReleaseError('inventory_size_limit')
        total+=e['size']
        if total>limits.max_total_bytes:raise ReleaseError('inventory_size_limit')
        if e['kind']=='file':
            if set(e)!={'path','kind','mode','size','sha256'} or e['mode'] not in (0o444,0o555) or not re.fullmatch('[a-f0-9]{64}',e.get('sha256','')):raise ReleaseError('inventory_schema')
        elif e['kind']=='directory':
            if set(e)!={'path','kind','mode','size'} or e['mode']!=0o555 or e['size']!=0:raise ReleaseError('inventory_schema')
        elif e['kind']=='symlink':
            if set(e)!={'path','kind','mode','size','target'} or e['mode']!=0o777:raise ReleaseError('inventory_schema')
            link_destination(e['path'],e['target'])
            if len(e['target'].encode())!=e['size']:raise ReleaseError('inventory_schema')
        else:raise ReleaseError('inventory_schema')
    if entries!=sorted(entries,key=lambda e:e['path'].encode('utf-8')):raise ReleaseError('unordered_inventory')
    by_path={e['path']:e for e in entries}
    for entry in entries:
        parent=PurePosixPath(entry['path']).parent
        if parent.as_posix()!='.' and (parent.as_posix() not in by_path or by_path[parent.as_posix()]['kind']!='directory'):raise ReleaseError('missing_inventory_parent')
    check_links(entries)
    return value


def _identity(version,revision,architecture):
    if not isinstance(version,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,95}',version) or version in ('.','..'):raise ReleaseError('release_version')
    if not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{40}',revision):raise ReleaseError('source_revision')
    if architecture not in ARCHES:raise ReleaseError('linux_architecture')


def _read_regular(path,maximum):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as file:
            info=os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>maximum:raise ReleaseError('unsafe_metadata_file')
            data=file.read(maximum+1)
            if len(data)>maximum:raise ReleaseError('metadata_size_limit')
            return data
    except OSError as error:raise ReleaseError('metadata_unavailable') from error


def verify_inventory(root,limits=Limits()):
    root=Path(root).absolute()
    data=_read_regular(root/'manifest.json',limits.max_manifest_bytes)
    manifest=validate_manifest(strict_json(data),limits)
    checksum=_read_regular(root/'SHA256SUMS',256)
    if any(stat.S_IMODE((root/name).lstat().st_mode)!=0o444 for name in META):raise ReleaseError('writable_seal_metadata')
    if data!=canonical(manifest):raise ReleaseError('noncanonical_manifest')
    if checksum!=f'{hashlib.sha256(data).hexdigest()}  manifest.json\n'.encode():raise ReleaseError('manifest_checksum')
    if inventory(root,limits)!=manifest['inventory']:raise ReleaseError('inventory_mismatch')
    if stat.S_IMODE(root.stat().st_mode)!=0o555:raise ReleaseError('writable_release')
    return manifest


def elf_architecture(path,expected):
    with Path(path).open('rb') as file:header=file.read(64)
    if len(header)<64 or header[:6]!=b'\x7fELF\x02\x01':raise ReleaseError('linux_elf_required')
    machine=struct.unpack_from('<H',header,18)[0]
    if expected not in ARCHES or machine!=ARCHES[expected]:raise ReleaseError('architecture_mismatch')
    return expected


def _private_directory(path):
    path=Path(os.path.abspath(path))
    if ':' in str(path) or any(ord(c)<32 for c in str(path)):raise ReleaseError('unsafe_destination_path')
    current=Path(path.anchor)
    for part in path.parts[1:]:
        current/=part
        if current.is_symlink():raise ReleaseError('symlinked_destination')
        if not current.exists():current.mkdir(mode=0o700)
        info=current.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0,os.geteuid()):raise ReleaseError('unowned_destination_ancestor')
        if current.name in ('.ori-runtime','.ssh','.aws') or any((current/name).exists() for name in ('sessions.sqlite3','.ori-runtime','core.lock','tasks.json')):raise ReleaseError('data_directory_not_destination')
        if info.st_mode&0o022 and not info.st_mode&stat.S_ISVTX:raise ReleaseError('writable_destination_ancestor')
    info=path.stat()
    if info.st_uid!=os.geteuid() or info.st_mode&0o022:raise ReleaseError('unowned_destination')
    if any((path/name).exists() for name in ('sessions.sqlite3','.ori-runtime','core.lock','tasks.json')):raise ReleaseError('data_directory_not_destination')
    return path


def _report(directory,operation,status,code=None,**details):
    directory=_private_directory(directory)
    value={'format':'ori-offline-core-diagnostic','version':1,'operation':operation,'status':status,**details}
    if code:value['code']=code
    # Never persist raw exceptions, argv, environment, child output or input data.
    event_path=directory/'events.ndjson'
    event_fd=os.open(event_path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
    with os.fdopen(event_fd,'ab') as events:
        import fcntl
        fcntl.flock(events.fileno(),fcntl.LOCK_EX)
        info=os.fstat(events.fileno());body=canonical(value)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or info.st_size+len(body)>4*1024*1024:raise ReleaseError('unsafe_diagnostic_history')
        events.write(body);events.flush();os.fsync(events.fileno())
    path=directory/'report.json'
    if path.is_symlink():raise ReleaseError('unsafe_diagnostics')
    flags=os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW
    fd=os.open(path,flags,0o600)
    with os.fdopen(fd,'wb') as file:
        info=os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():raise ReleaseError('unsafe_diagnostics')
        os.ftruncate(file.fileno(),0)
        file.write(canonical(value));file.flush();os.fsync(file.fileno())
    operation_path=directory/(operation+'-report.json')
    if operation_path.exists() or operation_path.is_symlink():
        existing=os.lstat(operation_path)
        if not stat.S_ISREG(existing.st_mode) or existing.st_nlink!=1:raise ReleaseError('unsafe_diagnostics')
    fd=os.open(operation_path,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as file:
        if os.fstat(file.fileno()).st_nlink!=1:raise ReleaseError('unsafe_diagnostics')
        os.ftruncate(file.fileno(),0);file.write(canonical(value));file.flush();os.fsync(file.fileno())


def _open_directory(source):
    path=Path(os.path.abspath(source));directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nested=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=nested
        result=directory;directory=None
        return result
    except OSError as error:raise ReleaseError('unsafe_source_path') from error
    finally:
        if directory is not None:os.close(directory)


def _open_source(source):
    path=Path(os.path.abspath(source));directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            nested=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=nested
        return os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
    except OSError as error:raise ReleaseError('unsafe_source_path') from error
    finally:os.close(directory)


def copy_regular(source,destination,expected_size=None):
    fd=_open_source(source)
    with os.fdopen(fd,'rb') as incoming:
        before=os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode):raise ReleaseError('special_file')
        if before.st_size>Limits().max_file_bytes:raise ReleaseError('file_size_limit')
        if expected_size is not None and before.st_size!=expected_size:raise ReleaseError('payload_changed')
        output=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(output,'wb') as out:
            digest=hashlib.sha256();count=0;tail=b''
            certificate=Path(source).suffix.lower() in ('.pem','.crt')
            while data:=incoming.read(CHUNK):
                if certificate:tail=_certificate_chunk(data,tail,count==0)
                count+=len(data)
                if count>before.st_size:raise ReleaseError('payload_changed')
                out.write(data);digest.update(data)
            after=os.fstat(incoming.fileno())
            if count!=before.st_size or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ReleaseError('payload_changed')
            out.flush();os.fsync(out.fileno())
        destination.chmod(0o555 if before.st_mode&0o111 else 0o444)
    return digest.hexdigest()


def _copy_tree(source,destination,limits):
    source=Path(source).absolute()
    entries=_scan(source,limits,hash_files=False,allow_hardlinks=True,skip_metadata=False)
    destination.mkdir(mode=0o700)
    for entry in entries:
        path=destination/entry['path']
        if entry['kind']=='directory':path.mkdir(mode=0o700)
        elif entry['kind']=='symlink':path.symlink_to(entry['target'])
        else:copy_regular(source/entry['path'],path,entry['size'])


def _fsync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def _seal_modes(root,seal_root=True):
    for directory,dirs,files in os.walk(root,followlinks=False):
        for name in files:
            path=Path(directory)/name
            if not path.is_symlink():path.chmod(0o555 if path.stat().st_mode&0o111 else 0o444)
    for directory,_,_ in os.walk(root,topdown=False,followlinks=False):
        if seal_root or Path(directory)!=root:Path(directory).chmod(0o555)
        _fsync_directory(directory)


def _remove_owned_stage(path):
    if not path.exists():return
    for directory,_,files in os.walk(path,followlinks=False):
        Path(directory).chmod(0o700)
    shutil.rmtree(path)


def publish_noreplace(stage,destination):
    libc=ctypes.CDLL(None,use_errno=True)
    try:function=libc.renameat2
    except AttributeError as error:raise ReleaseError('atomic_noreplace_unavailable') from error
    function.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint];function.restype=ctypes.c_int
    if function(-100,os.fsencode(stage),-100,os.fsencode(destination),1)!=0:
        code=ctypes.get_errno()
        if code==errno.EEXIST:raise FileExistsError(code,'release_exists')
        raise ReleaseError('atomic_publication_failed')
    _fsync_directory(Path(destination).parent)


def _spec(spec):
    required={'release_version','source_revision','architecture','ori_core','ori_supervisor','legacy_runtime','research_runtime','ui_assets','notices'}
    if not isinstance(spec,dict) or not required<=set(spec) or set(spec)-required-{'libraries','wheels'}:raise ReleaseError('input_spec_schema')
    _identity(spec['release_version'],spec['source_revision'],spec['architecture'])
    for key in ('ori_core','ori_supervisor'):
        item=spec[key]
        if not isinstance(item,dict) or not {'path','version','sha256'}<=set(item) or set(item)-{'path','version','sha256','build_revision'} or not re.fullmatch('[a-f0-9]{64}',item.get('sha256','')) or not re.fullmatch('[0-9][A-Za-z0-9.+_-]{0,63}',item.get('version','')):raise ReleaseError('component_spec')
        revision=item.get('build_revision')
        if revision is not None and (not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{40}',revision)):raise ReleaseError('component_build_revision')
        path=Path(item['path']).absolute()
        if path.is_symlink() or not path.is_file() or not path.stat().st_mode&0o111:raise ReleaseError('missing_executable')
        elf_architecture(path,spec['architecture'])
    for role in ('legacy','research'):
        item=spec[role+'_runtime']
        if not isinstance(item,dict) or set(item)!={'path','python_version','packages'}:raise ReleaseError('runtime_spec')
        if not re.fullmatch(r'3\.[0-9]+\.[0-9]+',item['python_version']) or (role=='research' and not item['python_version'].startswith('3.15.')):raise ReleaseError('runtime_version')
        if not isinstance(item['packages'],dict) or not REQUIRED_PACKAGES[role]<=set(item['packages']):raise ReleaseError('missing_runtime_dependency')
        if any(not re.fullmatch('[A-Za-z0-9_.+-]{1,80}',n) or not isinstance(v,str) or not re.fullmatch('[A-Za-z0-9_.+-]{1,80}',v) for n,v in item['packages'].items()):raise ReleaseError('runtime_package_version')
        root=Path(item['path']).absolute()
        if not (root/'bin/python3').is_file():raise ReleaseError('missing_executable')
    return spec


def _roots(spec):
    roots=[Path(spec[k]['path']).absolute() for k in ('legacy_runtime','research_runtime')]
    roots += [Path(spec[k]).absolute() for k in ('ui_assets','notices','libraries','wheels') if k in spec]
    return roots


def _manifest(spec,root,limits):
    return {'format':'ori-offline-core-release','manifest_version':1,
            **{key:spec[key] for key in ('release_version','source_revision','architecture')},
            'components':{name:{**{key:spec[name][key] for key in ('version','sha256')},'build_revision':spec[name].get('build_revision')} for name in ('ori_core','ori_supervisor')},
            'runtimes':{role:{key:spec[role+'_runtime'][key] for key in ('python_version','packages')} for role in ('legacy','research')},
            'wheelhouse':'optional_repair_artifacts' if 'wheels' in spec else 'not_supplied',
            'inventory':inventory(root,limits)}


def _write_manifest(root,value,limits):
    data=canonical(validate_manifest(value,limits))
    if len(data)>limits.max_manifest_bytes:raise ReleaseError('manifest_size_limit')
    for name,body in [('manifest.json',data),('SHA256SUMS',f'{hashlib.sha256(data).hexdigest()}  manifest.json\n'.encode())]:
        with (root/name).open('xb') as file:file.write(body);file.flush();os.fsync(file.fileno())
        (root/name).chmod(0o444)
    root.chmod(0o555)
    _fsync_directory(root)


def _external_diagnostics(directory,roots):
    location=Path(directory).absolute().resolve()
    for root in roots:
        root=Path(root).absolute().resolve()
        if location==root or root in location.parents:raise ReleaseError('diagnostics_overlap_payload')


def build(spec,releases,diagnostics,limits=Limits()):
    if isinstance(spec,dict):
        roots=[]
        for key in ('legacy_runtime','research_runtime'):
            value=spec.get(key)
            if isinstance(value,dict) and isinstance(value.get('path'),str):roots.append(value['path'])
        roots.extend(spec[key] for key in ('ui_assets','notices','libraries','wheels') if isinstance(spec.get(key),str))
        _external_diagnostics(diagnostics,roots)
    stage=None
    try:
        _spec(spec);limits.validate();releases=Path(releases).absolute()
        if any(releases==r or r in releases.parents for r in _roots(spec)):raise ReleaseError('source_destination_overlap')
        releases=_private_directory(releases);final=releases/spec['release_version']
        if final.exists() or final.is_symlink():raise ReleaseError('release_exists')
        total=0;entries=10;manifest_budget=4096
        for source in _roots(spec):
            scanned=_scan(source,limits,hash_files=False,allow_hardlinks=True,skip_metadata=False)
            total+=sum(e['size'] for e in scanned);entries+=len(scanned)
            manifest_budget+=sum(len(e['path'].encode())*2+576 for e in scanned)
            if manifest_budget>limits.max_manifest_bytes:raise ReleaseError('manifest_size_limit')
            if total>limits.max_total_bytes or entries>limits.max_entries:raise ReleaseError('inventory_size_limit')
        sizes={key:Path(spec[key]['path']).stat().st_size for key in ('ori_core','ori_supervisor')}
        if any(size>limits.max_file_bytes for size in sizes.values()) or total+sum(sizes.values())>limits.max_total_bytes:raise ReleaseError('inventory_size_limit')
        stage=Path(tempfile.mkdtemp(prefix='.ori-stage-',dir=releases))
        (stage/'bin').mkdir();(stage/'runtimes').mkdir()
        for key,name in [('ori_core','ori-core'),('ori_supervisor','ori-supervisor')]:
            checksum=copy_regular(Path(spec[key]['path']),stage/'bin'/name,sizes[key])
            if checksum!=spec[key]['sha256']:raise ReleaseError('component_hash_mismatch')
        for role in ('legacy','research'):_copy_tree(spec[role+'_runtime']['path'],stage/'runtimes'/role,limits)
        for key,name in [('ui_assets','ui'),('notices','notices'),('libraries','lib'),('wheels','wheels')]:
            if key in spec:_copy_tree(spec[key],stage/name,limits)
            else:(stage/name).mkdir()
        _seal_modes(stage,seal_root=False);_write_manifest(stage,_manifest(spec,stage,limits),limits)
        verify(stage,diagnostics,limits=limits)
        publish_noreplace(stage,final);stage=None
        _report(diagnostics,'build','passed',release_version=spec['release_version'],acceptance='relocated_linux_software_only')
        return final
    except BaseException as error:
        _report(diagnostics,'build','failed',str(error) if isinstance(error,ReleaseError) else type(error).__name__)
        if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
        if isinstance(error,ReleaseError):raise
        raise ReleaseError('build_failed') from error
    finally:
        if stage is not None:_remove_owned_stage(stage)

PYTHON_PROBE=r'''
import importlib,importlib.metadata,importlib.util,json,os,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve();role=sys.argv[2];expected=json.loads(sys.argv[3])
def inside(value):
    try:return Path(value).resolve().is_relative_to(root)
    except (OSError,ValueError):return False
if not all(inside(v) for v in (sys.executable,sys.prefix,sys.base_prefix)) or any(not inside(v) for v in sys.path if v):
    print(json.dumps({'ok':False,'code':'external_python_prefix'}));raise SystemExit(2)
modules={'legacy':['radioinspector.launcher','numpy','serial','ssl','sqlite3','ctypes'],
         'research':['ori_research.worker','radioinspector.measurements','numpy','duckdb','pyarrow','ssl','sqlite3','ctypes']}[role]
try:
    for name in modules:importlib.import_module(name)
    import ssl
    ssl.create_default_context()
    distributions={d.metadata['Name'].lower().replace('_','-'):d.version for d in importlib.metadata.distributions()}
    if distributions!=expected:raise ValueError('runtime_distribution_mismatch')
    paths=[]
    for module in list(sys.modules.values()):
        value=getattr(module,'__file__',None)
        if value and not value.startswith('<'):
            if not inside(value):raise ValueError('external_module_path')
            paths.append(Path(value).resolve().relative_to(root).as_posix())
    print(json.dumps({'ok':True,'python_version':'.'.join(map(str,sys.version_info[:3])),
          'implementation':sys.implementation.name,'prefix':'.','base_prefix':'.',
          'packages':distributions,'module_paths':sorted(set(paths)),
          'optional_sounddevice_installed':importlib.util.find_spec('sounddevice') is not None if role=='legacy' else None},sort_keys=True))
except Exception:
    print(json.dumps({'ok':False,'code':'runtime_import_or_version_failure'}));raise SystemExit(3)
'''


def _probe(argv,release,diagnostics,role=None,timeout=30):
    with tempfile.TemporaryDirectory(prefix='ori-offline-probe-') as directory:
        cwd=Path(directory)
        try:result=child.run(argv,cwd=cwd,env=child.environment(release,cwd,role),timeout=timeout)
        except (OSError,child.ProbeError) as error:raise ReleaseError('probe_execution_failed') from error
    return result


def probe_python(prefix,role,expected,diagnostics):
    prefix=Path(prefix).absolute();python=prefix/'bin/python3'
    if role not in REQUIRED_PACKAGES or not REQUIRED_PACKAGES[role]<=set(expected):raise ReleaseError('missing_runtime_dependency')
    # Reject a venv's external launcher before executing it, independently from
    # the in-child sys.base_prefix/module checks which prove real relocation.
    if not python.is_file() or not python.resolve().is_relative_to(prefix):raise ReleaseError('nonportable_python')
    for path in prefix.rglob('*.pth'):
        text=_read_regular(path,65536).decode('utf-8')
        if any(line.strip() and not line.lstrip().startswith('#') for line in text.splitlines()):raise ReleaseError('runtime_path_hook')
    for name in ('sitecustomize.py','usercustomize.py'):
        if next(prefix.rglob(name),None) is not None:raise ReleaseError('runtime_path_hook')
    release=prefix.parent.parent
    result=_probe([str(python),'-I','-B','-c',PYTHON_PROBE,str(prefix),role,json.dumps(expected,sort_keys=True)],release,diagnostics,role)
    try:value=strict_json(result.stdout)
    except ReleaseError as error:raise ReleaseError('python_probe_failed') from error
    if result.code!=0 or not isinstance(value,dict) or value.get('ok') is not True:raise ReleaseError('python_probe_failed')
    if value.get('implementation')!='cpython' or value.get('packages')!=expected:raise ReleaseError('python_probe_failed')
    return value


def _elf_dependencies(root,manifest,diagnostics):
    readelf=Path('/usr/bin/readelf')
    if not readelf.is_file():raise ReleaseError('missing_readelf')
    count=0;needed_abi=set()
    for item in manifest['inventory']:
        if item['kind']!='file':continue
        path=root/item['path']
        with path.open('rb') as file:magic=file.read(4)
        if magic!=b'\x7fELF':continue
        count+=1;elf_architecture(path,manifest['architecture'])
        result=_probe([str(readelf),'-l','-d','--wide',str(path)],root,diagnostics)
        if result.code:raise ReleaseError('invalid_elf')
        text=result.stdout.decode('utf-8',errors='strict')
        interpreters=re.findall(r'Requesting program interpreter: ([^\]]+)\]',text)
        expected_loader={'x86_64':'/lib64/ld-linux-x86-64.so.2','aarch64':'/lib/ld-linux-aarch64.so.1'}[manifest['architecture']]
        if any(value!=expected_loader for value in interpreters):raise ReleaseError('nonportable_elf_interpreter')
        paths=[]
        for raw in re.findall(r'Library (?:runpath|rpath): \[([^\]]*)\]',text):
            for value in raw.split(':'):
                value=value.replace('${ORIGIN}','$ORIGIN')
                if not value.startswith('$ORIGIN') or (len(value)>7 and value[7]!='/') or '$' in value[7:]:raise ReleaseError('nonportable_elf_rpath')
                candidate=(path.parent/value[8:]).resolve() if value.startswith('$ORIGIN/') else path.parent.resolve()
                if not candidate.is_relative_to(root):raise ReleaseError('escaping_elf_rpath')
                paths.append(candidate)
        paths.append(root/'lib')
        parts=PurePosixPath(item['path']).parts
        if len(parts)>2 and parts[0]=='runtimes':paths.append(root/'runtimes'/parts[1]/'lib')
        for library in re.findall(r'Shared library: \[([^\]]+)\]',text):
            if '/' in library or '\\' in library:
                origin=library.replace('${ORIGIN}','$ORIGIN')
                if not origin.startswith('$ORIGIN/') or '$' in origin[8:]:raise ReleaseError('nonportable_elf_dependency')
                target=(path.parent/origin[8:]).resolve()
                if not target.is_relative_to(root) or not target.is_file():raise ReleaseError('missing_native_dependency')
                continue
            if library in SYSTEM_ABI:needed_abi.add(library);continue
            if not any((directory/library).is_file() and (directory/library).resolve().is_relative_to(root) for directory in paths):raise ReleaseError('missing_native_dependency')
    if count<4:raise ReleaseError('missing_elf_components')
    return {'elf_files':count,'system_abi_libraries':sorted(needed_abi)}


class _Assets(HTMLParser):
    def __init__(self):super().__init__();self.paths=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='script' and attrs.get('src'):self.paths.append(attrs['src'])
        if tag=='link' and attrs.get('rel') in ('stylesheet','modulepreload','icon') and attrs.get('href'):self.paths.append(attrs['href'])


def _verify_ui(root):
    data=_read_regular(root/'ui/index.html',1024*1024)
    parser=_Assets();parser.feed(data.decode('utf-8'))
    if not parser.paths:raise ReleaseError('missing_offline_ui_assets')
    for name in parser.paths:
        if re.match(r'^[a-z]+:',name,re.I) or name.startswith('//') or '?' in name or '#' in name:raise ReleaseError('remote_ui_dependency')
        name=name.removeprefix('/modern/').removeprefix('./')
        safe_path(name)
        if not (root/'ui'/name).is_file():raise ReleaseError('missing_offline_ui_asset')


def _verify_wheels(root,limits):
    for wheel in (root/'wheels').rglob('*'):
        if not wheel.is_file() or wheel.is_symlink():continue
        if wheel.suffix!='.whl':raise ReleaseError('unexpected_repair_artifact')
        try:
            with zipfile.ZipFile(wheel) as archive:
                seen=set();size=0
                for info in archive.infolist():
                    name=info.filename.rstrip('/')
                    private_path(name)
                    if name in seen:raise ReleaseError('duplicate_wheel_member')
                    seen.add(name);size+=info.file_size
                    mode=info.external_attr>>16
                    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR)):raise ReleaseError('special_wheel_member')
                    if len(seen)>limits.max_entries or size>limits.max_total_bytes or info.file_size>limits.max_file_bytes:raise ReleaseError('wheel_expansion_limit')
                    count=0;tail=b'';certificate=Path(name).suffix.lower() in ('.pem','.crt')
                    with archive.open(info) as member:
                        while data:=member.read(CHUNK):
                            if certificate:tail=_certificate_chunk(data,tail,count==0)
                            count+=len(data)
                            if count>info.file_size:raise ReleaseError('wheel_expansion_limit')
                    if count!=info.file_size:raise ReleaseError('invalid_repair_wheel')
        except zipfile.BadZipFile as error:raise ReleaseError('invalid_repair_wheel') from error


def verify(root,diagnostics,limits=Limits()):
    root=Path(root).absolute()
    _external_diagnostics(diagnostics,[root])
    try:
        manifest=verify_inventory(root,limits)
        if platform.system()!='Linux' or platform.machine()!=manifest['architecture']:raise ReleaseError('native_linux_probe_required')
        for name,key in [('ori-core','ori_core'),('ori-supervisor','ori_supervisor')]:
            path=root/'bin'/name
            if not path.is_file() or not path.stat().st_mode&0o111:raise ReleaseError('missing_executable')
            actual=next((e for e in manifest['inventory'] if e['path']==f'bin/{name}'),None)
            if not actual or actual['sha256']!=manifest['components'][key]['sha256']:raise ReleaseError('component_hash_mismatch')
        for name in ('LICENSE.txt','PYTHON-LICENSE.txt'):
            if not (root/'notices'/name).is_file():raise ReleaseError('missing_notices')
        _verify_ui(root);_verify_wheels(root,limits)
        native=_elf_dependencies(root,manifest,diagnostics)
        runtimes={}
        for role in ('legacy','research'):
            expected=manifest['runtimes'][role]
            observed=probe_python(root/'runtimes'/role,role,expected['packages'],diagnostics)
            if observed['python_version']!=expected['python_version']:raise ReleaseError('python_version_mismatch')
            runtimes[role]=observed
        result=_probe([str(root/'bin/ori-core'),'--version'],root,diagnostics)
        expected=f"ori-core {manifest['components']['ori_core']['version']} candidate"
        if result.code or result.stdout.decode().strip()!=expected:raise ReleaseError('core_version_probe_failed')
        result=_probe([str(root/'bin/ori-core'),'--help'],root,diagnostics)
        if result.code or b'shadow' not in result.stdout or b'replay' not in result.stdout:raise ReleaseError('core_help_probe_failed')
        # Current supervisor deliberately has no invented version flag. Its
        # supplied build version is bound to its sealed binary hash; no-argv
        # usage is a read-only native-loader probe, not startup or readiness.
        result=_probe([str(root/'bin/ori-supervisor')],root,diagnostics)
        if result.code!=1 or b'usage: ori-supervisor run' not in result.stderr:raise ReleaseError('supervisor_usage_probe_failed')
        verify_inventory(root,limits)
        report={'acceptance':'relocated_linux_software_only','release_version':manifest['release_version'],
                'architecture':manifest['architecture'],'component_build_revisions':{k:v.get('build_revision') for k,v in manifest['components'].items()},'runtime_versions':{r:v['python_version'] for r,v in runtimes.items()},
                'optional_legacy_sounddevice_installed':runtimes['legacy']['optional_sounddevice_installed'],**native}
        _report(diagnostics,'verify','passed',**report)
        return manifest
    except BaseException as error:
        _report(diagnostics,'verify','failed',str(error) if isinstance(error,ReleaseError) else type(error).__name__)
        if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
        if isinstance(error,ReleaseError):raise
        raise ReleaseError('verification_failed') from error


def install(bundle,releases,diagnostics,limits=Limits()):
    _external_diagnostics(diagnostics,[bundle])
    stage=None
    try:
        bundle=Path(bundle).absolute();manifest=verify(bundle,diagnostics,limits)
        releases=Path(releases).absolute()
        if bundle==releases or bundle in releases.parents:raise ReleaseError('source_destination_overlap')
        releases=_private_directory(releases);final=releases/manifest['release_version']
        if final.exists() or final.is_symlink():raise ReleaseError('release_exists')
        stage=Path(tempfile.mkdtemp(prefix='.ori-install-',dir=releases))
        # Copy the sealed payload and metadata to a different absolute prefix;
        # probes must pass again here before publication at the new location.
        for item in manifest['inventory']:
            destination=stage/item['path']
            if item['kind']=='directory':destination.mkdir(mode=0o700)
            elif item['kind']=='symlink':destination.symlink_to(item['target'])
            else:copy_regular(bundle/item['path'],destination,item['size'])
        for name in sorted(META):copy_regular(bundle/name,stage/name)
        _seal_modes(stage);verify(stage,diagnostics,limits)
        publish_noreplace(stage,final);stage=None
        _report(diagnostics,'install','passed',release_version=manifest['release_version'],selection_changed=False,data_touched=False)
        return final
    except BaseException as error:
        _report(diagnostics,'install','failed',str(error) if isinstance(error,ReleaseError) else type(error).__name__)
        if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
        if isinstance(error,ReleaseError):raise
        raise ReleaseError('installation_failed') from error
    finally:
        if stage is not None:_remove_owned_stage(stage)


def _supervisor_data_path(data):
    data=Path(data)
    if not data.is_absolute():raise ReleaseError('template_parameters')
    if len(os.fsencode(data/'.ori-runtime/supervisor.sock'))>=108:raise ReleaseError('supervisor_socket_path_too_long')
    return data


def backend_spec(release,data,port):
    release=Path(release).absolute();data=_supervisor_data_path(data)
    if not data.is_absolute() or not 1<=port<=65535:raise ReleaseError('template_parameters')
    return {'name':'legacy-python','program':str(release/'runtimes/legacy/bin/python3'),
            'args':['-I','-B','-c',CORE_MAIN,'--data-dir',str(data),'--host','127.0.0.1','--port',str(port)],
            'environment':{'LD_LIBRARY_PATH':f'{release}/runtimes/legacy/lib:{release}/lib'},
            'control_addr':f'127.0.0.1:{port}','ports':[f'127.0.0.1:{port}'],
            'startup_timeout_ms':120000,'shutdown_timeout_ms':120000}


def render(release,data,port,output,previous=None):
    release=Path(release).absolute();verify_inventory(release)
    output=Path(output).absolute();data=_supervisor_data_path(Path(data).absolute())
    if output==data or data in output.parents or output==release or release in output.parents:raise ReleaseError('template_output_overlaps_data_or_release')
    if type(port) is not int or not 1<=port<65535:raise ReleaseError('template_parameters')
    if output.exists() or output.is_symlink():raise ReleaseError('template_output_exists')
    _private_directory(output.parent)
    backend=backend_spec(release,data,port)
    files={'supervisor.json':{'data_directory':str(data),'backend':backend},'legacy-backend.json':backend,
           'rust-candidate.json':{'name':'rust-shadow-candidate','program':str(release/'bin/ori-core'),
            'args':['--mode','shadow','--data-dir',str(data),'--host','127.0.0.1','--port',str(port),'--control-port',str(port+1)],
            'environment':{'LD_LIBRARY_PATH':f'{release}/lib'},'control_addr':f'127.0.0.1:{port+1}',
            'ports':[f'127.0.0.1:{port}',f'127.0.0.1:{port+1}'],'startup_timeout_ms':120000,'shutdown_timeout_ms':120000}}
    if previous is not None:
        verify_inventory(Path(previous));files['rollback-backend.json']=backend_spec(previous,data,port)
    stage=Path(tempfile.mkdtemp(prefix='.ori-config-',dir=output.parent))
    try:
        for name,value in files.items():
            with (stage/name).open('xb') as file:file.write(canonical(value));file.flush();os.fsync(file.fileno())
            (stage/name).chmod(0o600)
        _fsync_directory(stage);publish_noreplace(stage,output);stage=None
        return output
    finally:
        if stage is not None:_remove_owned_stage(stage)


def check_released_listener(port):
    # A closed HTTP listener can leave TIME_WAIT connections. Match the
    # service's normal reusable TCP listener without accepting a live listener.
    try:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            listener.bind(('127.0.0.1',port));listener.listen(1)
    except OSError as error:raise ReleaseError('listener_still_owned') from error


def _loopback_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));return listener.getsockname()[1]


def _api(port,method,path,payload=None):
    from http.client import HTTPConnection
    connection=HTTPConnection('127.0.0.1',port,timeout=10)
    try:
        body=None if payload is None else json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
        connection.request(method,path,body,{'Content-Type':'application/json'} if body is not None else {})
        response=connection.getresponse();raw=response.read(2*1024*1024+1)
        if len(raw)>2*1024*1024 or response.status not in (200,201):raise ReleaseError('rehearsal_api_failed')
        value=strict_json(raw)
        if not isinstance(value,dict) or value.get('contract')!='openradioinspector-platform' or value.get('contract_version')!=1:raise ReleaseError('rehearsal_contract_failed')
        return value['data']
    finally:connection.close()


def _admin(supervisor,data,operation,root,diagnostics,extra=()):
    result=_probe([str(supervisor),operation,str(data),*map(str,extra)],root,diagnostics,timeout=150)
    if result.code:raise ReleaseError('supervisor_admin_failed')
    value=strict_json(result.stdout)
    if not isinstance(value,dict) or value.get('ok') is not True:raise ReleaseError('supervisor_admin_failed')
    return value


def rehearse(previous,candidate,scratch,diagnostics,limits=Limits()):
    """Explicit foreground children on new private data; never system services."""
    import subprocess
    from urllib.parse import quote
    scratch=Path(scratch).absolute();process=None;cleaned=False
    _supervisor_data_path(scratch/'data')
    if scratch.exists() or scratch.is_symlink():raise ReleaseError('rehearsal_directory_exists')
    _private_directory(scratch.parent);scratch.mkdir(mode=0o700)
    try:
        old=install(previous,scratch/'releases',Path(diagnostics)/'install-previous',limits)
        new=install(candidate,scratch/'releases',Path(diagnostics)/'install-candidate',limits)
        if old==new:raise ReleaseError('rehearsal_requires_distinct_versions')
        data=scratch/'data';data.mkdir(mode=0o700);port=_loopback_port()
        configuration=scratch/'supervisor.json'
        configuration.write_bytes(canonical({'data_directory':str(data),'backend':backend_spec(old,data,port)}));configuration.chmod(0o600)
        supervisor=old/'bin/ori-supervisor'
        process=subprocess.Popen([str(supervisor),'run',str(configuration)],cwd=scratch,
                env=child.environment(old,scratch),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,shell=False,start_new_session=True,close_fds=True)
        deadline=time.monotonic()+125
        while True:
            if child.exited(process):raise ReleaseError('supervisor_exited_before_ready')
            try:
                status=_admin(supervisor,data,'status',old,diagnostics)
                if status['status']['ready'] and status['status']['writer_owned'] and status['status']['preflight_complete']:break
            except ReleaseError:
                if time.monotonic()>=deadline:raise ReleaseError('supervisor_startup_timeout')
                time.sleep(.05)
        sid='offline-rehearsal/session-e\u0301';stream='original/spectrum+one';route='/api/v1/sessions/'+quote(sid,safe='')
        _api(port,'POST','/api/v1/sessions',{'id':sid,'name':'Owned offline release rehearsal',
            'streams':[{'id':stream,'device':'offline-fixture-no-device','role':'spectrum_analyzer','level_unit':'dBm'}]})
        def measurement(identifier,level):
            return {'id':identifier,'session_id':sid,'stream_id':stream,'interval':{'started_at':'2026-11-11T12:00:00.123456Z','ended_at':'2026-11-11T12:00:00.223456Z'},
                'frequencies_hz':[100000001,100000002],'levels':[-0.0,level],'level_unit':'dBm',
                'settings':{'source':'synthetic-measurement-only','hardware_validated':False}}
        first_id='before/update-α';second_id='latest/after-update-β'
        _api(port,'POST',route+'/sweeps',measurement(first_id,-1.0000000000000002))
        before=_api(port,'GET',route+'/sweeps?limit=10')
        candidate_file=scratch/'candidate.json';candidate_file.write_bytes(canonical(backend_spec(new,data,port)));candidate_file.chmod(0o600)
        _admin(supervisor,data,'cutover',old,diagnostics,(candidate_file,))
        if canonical(_api(port,'GET',route+'/sweeps?limit=10'))!=canonical(before):raise ReleaseError('measurement_changed_on_update')
        _api(port,'POST',route+'/sweeps',measurement(second_id,-99.125))
        latest=_api(port,'GET',route+'/sweeps?limit=10')
        rollback_file=scratch/'rollback.json';rollback_file.write_bytes(canonical(backend_spec(old,data,port)));rollback_file.chmod(0o600)
        _admin(supervisor,data,'cutover',old,diagnostics,(rollback_file,))
        after=_api(port,'GET',route+'/sweeps?limit=10')
        if canonical(after)!=canonical(latest) or {v['id'] for v in after}!={first_id,second_id}:raise ReleaseError('latest_measurements_not_preserved')
        by_id={v['id']:v for v in after}
        if struct.pack('>d',by_id[first_id]['levels'][0]).hex()!='8000000000000000' or struct.pack('>d',by_id[first_id]['levels'][1]).hex()!='bff0000000000001':raise ReleaseError('measurement_float_bits_changed')
        _admin(supervisor,data,'shutdown',old,diagnostics)
        deadline=time.monotonic()+125
        while not child.exited(process):
            if time.monotonic()>=deadline:raise ReleaseError('supervisor_shutdown_timeout')
            time.sleep(.05)
        if child.live_group(process.pid):raise ReleaseError('supervisor_left_descendants')
        if process.wait()!=0:raise ReleaseError('supervisor_shutdown_failed')
        cleaned=True
        import fcntl
        for name in ('writer.lock','instance.lock'):
            with (data/'.ori-runtime'/name).open('rb') as lock:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
        check_released_listener(port)
        selection=strict_json(_read_regular(data/'.ori-runtime/selection.json',1024*1024))
        if selection['program']!=str(old/'runtimes/legacy/bin/python3'):raise ReleaseError('rollback_selection_not_preserved')
        report={'acceptance':'owned_linux_software_rehearsal_only','previous_release':old.name,'candidate_release':new.name,
                'component_build_revisions':{k:v.get('build_revision') for k,v in verify_inventory(new)['components'].items()},
                'latest_measurement_count':len(after),'latest_measurements_sha256':hashlib.sha256(canonical(after)).hexdigest(),
                'writer_lease_released':True,'instance_lease_released':True,'owned_children_reaped':True,
                'system_service_changed':False,'existing_data_touched':False,'rust_promoted':False}
        _report(diagnostics,'rehearse','passed',**report)
        return report
    except BaseException as error:
        _report(diagnostics,'rehearse','failed',str(error) if isinstance(error,ReleaseError) else type(error).__name__)
        if isinstance(error,(KeyboardInterrupt,SystemExit)):raise
        if isinstance(error,ReleaseError):raise
        raise ReleaseError('rehearsal_failed') from error
    finally:
        if process is not None and not cleaned:child.stop_owned(process)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='operation',required=True)
    p=sub.add_parser('build',help='Consume prepared local payloads; mandatory real relocation probes')
    p.add_argument('--spec',type=Path,required=True);p.add_argument('--releases',type=Path,required=True);p.add_argument('--report-dir',type=Path,required=True)
    for operation in ('verify','inspect'):
        p=sub.add_parser(operation,help='Read-only payload inspection' if operation=='inspect' else 'Full real relocation verification')
        p.add_argument('release',type=Path);p.add_argument('--report-dir',type=Path,required=True)
    p=sub.add_parser('install',help='Publish a NEW version directory; no service/pointer/data mutation')
    p.add_argument('release',type=Path);p.add_argument('--releases',type=Path,required=True);p.add_argument('--report-dir',type=Path,required=True)
    p=sub.add_parser('render',help='Write new concrete Supervisor config files; never start anything')
    p.add_argument('release',type=Path);p.add_argument('--data-dir',type=Path,required=True);p.add_argument('--port',type=int,default=8770);p.add_argument('--output',type=Path,required=True);p.add_argument('--previous',type=Path)
    p=sub.add_parser('rehearse',help='Explicit NEW private-data foreground Supervisor cutover/rollback')
    p.add_argument('--previous',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True);p.add_argument('--scratch',type=Path,required=True);p.add_argument('--report-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if platform.system()!='Linux':raise ReleaseError('linux_only')
        if args.operation=='build':result=build(strict_json(_read_regular(args.spec,1024*1024)),args.releases,args.report_dir)
        elif args.operation=='verify':result=verify(args.release,args.report_dir)['release_version']
        elif args.operation=='inspect':
            _external_diagnostics(args.report_dir,[args.release])
            manifest=verify_inventory(args.release)
            result={'release_version':manifest['release_version'],'acceptance':'inventory_only_not_portability'}
            _report(args.report_dir,'inspect','passed',**result)
        elif args.operation=='install':result=install(args.release,args.releases,args.report_dir)
        elif args.operation=='render':result=render(args.release,args.data_dir,args.port,args.output,args.previous)
        else:result=rehearse(args.previous,args.candidate,args.scratch,args.report_dir)
        print(json.dumps(result if isinstance(result,dict) else {'result':str(result)},sort_keys=True))
        return 0
    except (ReleaseError,OSError) as error:
        print('offline-core: '+(str(error) if isinstance(error,ReleaseError) else 'filesystem_operation_failed'),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
