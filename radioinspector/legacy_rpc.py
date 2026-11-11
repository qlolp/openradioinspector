"""One-shot legacy scientific bridge. Only a private snapshot is ever opened.

The original ApplicationState and legacy HTTP helpers own all scientific math.
The caller alone owns the real .pan writer, source revision and HTTP transport.
"""
from __future__ import annotations

import os
for _key in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS","NUMEXPR_NUM_THREADS","VECLIB_MAXIMUM_THREADS","ARROW_NUM_THREADS"):
    os.environ[_key]="1"

import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
from urllib.parse import urlencode

from radioinspector.research_rpc import (DEFAULT_LIMITS, ProtocolError, _pairs, _constant,
    _json_values, _valid_request_id, _failure, _bounded, _encode)

MAX_REQUEST_BYTES=16*1024**2
MAX_RESPONSE_BYTES=16*1024**2
DEFAULT_LEGACY_LIMITS={
    "max_database_bytes":128*1024**2,"max_rows":10_000,"max_total_points":1_000_000,
    "max_record_bytes":8*1024**2,"max_result_bytes":16*1024**2,"max_sidecar_bytes":16*1024**2,
}
ACTIONS=frozenset({"load","state","signals","coverage","occupancy","efficiency","measurements","keywords","threshold"})


def _strict(raw):
    try:
        result=json.loads(raw.decode('utf-8',errors='strict'),object_pairs_hook=_pairs,parse_constant=_constant)
        _json_values(result)
        return result
    except (UnicodeError,ValueError,RecursionError) as error:
        if isinstance(error,ProtocolError):raise
        raise ProtocolError('Request must be one strict UTF-8 JSON object') from error


def _limits(value,defaults,label):
    if not isinstance(value,dict) or set(value)-set(defaults):raise ProtocolError(f'Unknown {label}')
    result=dict(defaults)
    for key,number in value.items():
        if type(number) is not int or not 1<=number<=defaults[key]:raise ProtocolError(f'{label} may only lower the existing limits')
        result[key]=number
    return result


def _validate(request):
    allowed={'version','request_id','operation','method','action','snapshot_directory','snapshot_nonce','database','source_version','state','parameters','legacy_limits','limits','shared_io'}
    required=allowed-{'snapshot_directory','snapshot_nonce','limits','shared_io'}
    if not isinstance(request,dict) or set(request)-allowed or required-set(request):raise ProtocolError('Invalid legacy request fields')
    if type(request['version']) is not int or request['version']!=1 or request['operation']!='analysis' or request['method']!='legacy_workspace':raise ProtocolError('Unsupported legacy protocol')
    if not _valid_request_id(request['request_id']) or request['action'] not in ACTIONS:raise ProtocolError('Unsupported legacy action or request ID')
    if type(request['source_version']) is not int or not 0<=request['source_version']<2**64:raise ProtocolError('Invalid source version')
    if not isinstance(request['state'],dict) or set(request['state'])!={'threshold','signals'} or not isinstance(request['state']['signals'],list):raise ProtocolError('Invalid working state')
    if not isinstance(request['parameters'],dict):raise ProtocolError('Parameters must be an object')
    if request['action']=='threshold' and len(json.dumps(request['parameters'],ensure_ascii=False,allow_nan=False,separators=(',',':')).encode())>65536:
        raise ProtocolError('Threshold control byte limit exceeded',code='resource_limit')
    database=request['database']
    if database is not None and (not isinstance(database,str) or not database or '\0' in database):raise ProtocolError('Invalid display database identity')
    if request['action'] in {'load','threshold'} and request['source_version']==2**64-1:raise ProtocolError('Legacy version exhausted',code='resource_limit')
    _limits(request.get('limits',{}),DEFAULT_LIMITS,'execution limits')
    return _limits(request['legacy_limits'],DEFAULT_LEGACY_LIMITS,'legacy limits')


def _regular(path,maximum):
    try:info=path.lstat()
    except OSError as error:raise ProtocolError('Private snapshot is unavailable') from error
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>maximum:raise ProtocolError('Invalid private snapshot file')
    if os.name=='posix' and (info.st_uid!=os.geteuid() or info.st_mode&0o077):raise ProtocolError('Snapshot is not private to the worker owner')
    return path


def _read_file(path,maximum,shared_io):
    """Read an owner-validated, bounded file without an unmetered bulk read."""
    size=path.stat().st_size
    if size>maximum:raise ProtocolError('Private snapshot file budget exceeded',code='resource_limit')
    pieces=[]
    with path.open('rb') as source:
        remaining=size
        while remaining:
            count=min(65536,remaining)
            shared_io.account(count)
            block=source.read(count)
            if len(block)!=count:raise ProtocolError('Private snapshot changed')
            pieces.append(block);remaining-=count
        if source.read(1):raise ProtocolError('Private snapshot changed')
    return b''.join(pieces)


def _copy_file(source,destination,maximum,shared_io):
    size=source.stat().st_size
    if size>maximum:raise ProtocolError('Private snapshot file budget exceeded',code='resource_limit')
    with source.open('rb') as reader,destination.open('xb') as writer:
        remaining=size
        while remaining:
            count=min(65536,remaining)
            shared_io.account(count)
            block=reader.read(count)
            if len(block)!=count:raise ProtocolError('Private snapshot changed')
            shared_io.account(count)
            if writer.write(block)!=count:raise ProtocolError('Private snapshot write failed')
            remaining-=count
        if reader.read(1):raise ProtocolError('Private snapshot changed')


def _snapshot_root(request):
    directory=request.get('snapshot_directory')
    if not isinstance(directory,str) or not directory or '\0' in directory:raise ProtocolError('Private snapshot directory required')
    root=Path(directory)
    try:info=root.lstat();resolved=root.resolve(strict=True)
    except OSError as error:raise ProtocolError('Private snapshot is unavailable') from error
    if not root.is_absolute() or root!=resolved or not stat.S_ISDIR(info.st_mode):raise ProtocolError('Invalid private snapshot directory')
    if os.name=='posix' and (info.st_uid!=os.geteuid() or info.st_mode&0o077):raise ProtocolError('Snapshot directory is not private')
    return root


def _copy_snapshot(request,limits,destination,shared_io=None):
    root=_snapshot_root(request)
    manifest_path=_regular(root/'snapshot.json',4096)
    manifest=_strict(_read_file(manifest_path,4096,shared_io) if shared_io is not None else manifest_path.read_bytes())
    if not isinstance(manifest,dict) or set(manifest)!={'version','nonce','panorama_file','signals_file'} or type(manifest['version']) is not int or manifest['version']!=1 or not isinstance(manifest['nonce'],str) or not 1<=len(manifest['nonce'])<=128 or manifest['nonce']!=request.get('snapshot_nonce') or manifest['panorama_file']!='workspace.pan' or manifest['signals_file'] not in (None,'signals.json'):
        raise ProtocolError('Invalid private snapshot seal')
    source=_regular(root/'workspace.pan',limits['max_database_bytes'])
    # Source is read-only; even PanoramaStore's schema/WAL initialization and
    # threshold writes run only on this second, worker-owned disposable copy.
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as reader:
        reader.execute('PRAGMA query_only=ON')
        rows,total,largest=reader.execute('SELECT COUNT(*),COALESCE(SUM(length(levels)),0),COALESCE(MAX(length(levels)),0) FROM panorama').fetchone()
        if rows>limits['max_rows'] or total>limits['max_total_points']*8 or largest>limits['max_record_bytes']:
            raise ProtocolError('Legacy source budget exceeded',code='resource_limit')
        if request['action']=='threshold' and reader.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise ProtocolError('Threshold writes with custom triggers are not permitted')
        if request['action']=='threshold' and reader.execute('SELECT 1 FROM sqlite_master AS m, pragma_foreign_key_list(m.name) AS f WHERE m.type=\'table\' AND f."table"=\'threshold_line\' COLLATE NOCASE LIMIT 1').fetchone():
            raise ProtocolError('Threshold writes with custom foreign-key relations are not permitted')
        if shared_io is not None:
            page_size=reader.execute('PRAGMA page_size').fetchone()[0]
            total=reader.execute('PRAGMA page_count').fetchone()[0]
            if total*page_size>limits['max_database_bytes']:
                raise ProtocolError('Legacy source budget exceeded',code='resource_limit')
        with closing(sqlite3.connect(destination/'workspace.pan')) as writer:
            if shared_io is None:reader.backup(writer,pages=128)
            else:
                pages=max(1,65536//page_size)
                previous=total
                def reserve(remaining):
                    count=min(pages,remaining)*page_size
                    shared_io.account(count);shared_io.account(count)
                def progress(_status,remaining,count):
                    nonlocal previous
                    if count!=total or not 0<=remaining<=previous:raise ProtocolError('Private snapshot changed')
                    if remaining<previous:
                        previous=remaining
                        if remaining:reserve(remaining)
                reserve(total)
                reader.backup(writer,pages=pages,progress=progress)
    if manifest['signals_file'] is not None:
        sidecar=_regular(root/'signals.json',limits['max_sidecar_bytes'])
        if shared_io is None:shutil.copyfile(sidecar,destination/'signals.json')
        else:_copy_file(sidecar,destination/'signals.json',limits['max_sidecar_bytes'],shared_io)
    return destination/'workspace.pan'


class _Rows:
    def __init__(self,rows):self.rows=rows
    def to_rows(self):return self.rows


class _Capture:
    def __init__(self,state,path=''):
        self.state=state;self.path=path;self.runtime_bridge=None;self.response=None
    def _runtime_request(self,_method):return False
    def _send_json(self,value,status=200):self.response=(status,value)
    def _get(self):
        from radioinspector.web.server import Handler
        return Handler._get(self)
    def _send_api_error(self,*_):raise ProtocolError('Only the legacy API is permitted')


class _LegacyError(ValueError):
    def __init__(self,message,status=400):super().__init__(message);self.status=status


def _query(state,action,parameters):
    from radioinspector.web.server import Handler
    if action=='state':
        if set(parameters)-{'include_waterfall'} or type(parameters.get('include_waterfall',True)) is not bool:raise ProtocolError('Invalid state parameters')
        return state.snapshot(parameters.get('include_waterfall',True))
    routes={'signals':('/api/signals',set()),'coverage':('/api/coverage',{'rows','cols'}),
        'occupancy':('/api/occupancy',{'threshold'}),'efficiency':('/api/efficiency',{'threshold'}),
        'measurements':('/api/measure',{'start','stop','norm'}),'keywords':('/api/keywords',set())}
    route,fields=routes[action]
    if set(parameters)-fields:raise ProtocolError('Unknown query parameters')
    if any(not isinstance(value,(str,int,float)) or isinstance(value,bool) for value in parameters.values()):raise ProtocolError('Invalid scalar query parameters')
    capture=_Capture(state,route+('?' + urlencode(parameters) if parameters else ''))
    Handler.do_GET(capture)
    status,result=capture.response
    if status!=200:raise _LegacyError(result['error'],status)
    return result


def _execute(request,limits,shared_io=None):
    from radioinspector.jsonio import to_jsonable
    from radioinspector.web.server import ApplicationState,Handler
    from radioinspector.panorama import PanoramaStore
    from radioinspector.threshold import ThresholdLine
    import numpy as np
    action=request['action'];parameters=request['parameters']
    # A supervisor that reaps a terminated child can remove its entire owned
    # snapshot tree, including any interrupted second copy. Ordinary calls keep
    # the original temporary-directory behavior.
    owner=_snapshot_root(request) if shared_io is not None and request.get('snapshot_directory') is not None else None
    with tempfile.TemporaryDirectory(prefix='ori-legacy-science-',dir=owner) as private:
        private=Path(private)
        path=_copy_snapshot(request,limits,private,shared_io) if request.get('snapshot_directory') is not None else None
        if (request['database'] is None)!=(path is None):raise ProtocolError('Snapshot/display database mismatch')
        state=ApplicationState(library=str(private))
        try:
            state._version=request['source_version']
            if action=='load':
                if path is None or parameters:raise ProtocolError('Load requires a private snapshot and no parameters')
                # The unchanged loader reads its copied JSON sidecar once. Its
                # exact file length is reserved in bounded logical units first;
                # this is not a claim about internal library/OS syscall sizes.
                if shared_io is not None and (private/'signals.json').is_file():
                    shared_io.account((private/'signals.json').stat().st_size)
                state.load_database(str(path));state.database=request['database']
                reply={'ok':True,'version':state.version}
            else:
                if path is not None:
                    state.store=PanoramaStore(path);state.database=request['database']
                threshold=request['state']['threshold']
                if threshold is not None:
                    if not isinstance(threshold,dict) or set(threshold)!={'index','unit','frequencies','levels'}:raise ProtocolError('Invalid working threshold')
                    state.threshold=ThresholdLine(np.asarray(threshold['frequencies'],dtype=float),np.asarray(threshold['levels'],dtype=float),threshold['index'],threshold['unit'])
                state.signals=_Rows(request['state']['signals'])
                if action=='threshold':
                    capture=_Capture(state)
                    Handler._dispatch_post(capture,'/api/threshold',parameters)
                    status,reply=capture.response
                    if status!=200:raise _LegacyError(reply['error'],status)
                else:reply=_query(state,action,parameters)
            result={'reply':reply}
            if action in {'load','threshold'}:result['state']=state.snapshot()
            return to_jsonable(result)
        finally:state.close()


def process_request(raw):
    request_id=None
    shared_io=None
    try:
        if not isinstance(raw,bytes):raise ProtocolError('Request must be bytes')
        if len(raw)>MAX_REQUEST_BYTES:raise ProtocolError('Request byte limit exceeded',code='resource_limit')
        request=_strict(raw)
        if isinstance(request,dict) and _valid_request_id(request.get('request_id')):request_id=request['request_id']
        limits=_validate(request)
        if 'shared_io' in request:
            from radioinspector.research_rpc import _shared_file_io
            shared_io=_shared_file_io(request)
        result=_execute(request,limits,shared_io) if shared_io is not None else _execute(request,limits)
        response={'version':1,'request_id':request_id,'operation':'analysis','ok':True,'result':result}
        if shared_io is not None:response['shared_io_receipt']=shared_io.receipt()
        if len(_encode(response))>limits['max_result_bytes']:raise ProtocolError('Legacy result budget exceeded',code='resource_limit')
    except Exception as error:
        response=_failure(request_id,error)
        if isinstance(error,_LegacyError):
            response['error'].update(http_status=error.status,api_message=str(error),api_code='legacy_workspace_error')
        elif isinstance(error,sqlite3.Error):
            response['error'].update(code='validation_error',type=type(error).__name__,message=str(error),http_status=400,api_message=str(error),api_code='legacy_workspace_error')
    finally:
        if shared_io is not None:shared_io.close()
    return _bounded(response)


def run(source,destination):
    pieces=[];length=0
    try:
        while length<=MAX_REQUEST_BYTES:
            piece=source.read(min(65536,MAX_REQUEST_BYTES+1-length))
            if not piece:break
            pieces.append(piece);length+=len(piece)
        response=process_request(b''.join(pieces))
    except (OSError,ValueError):response=_failure(None,ProtocolError('Unable to read request stream'))
    try:destination.write(_encode(response));destination.flush()
    except (BrokenPipeError,OSError):return 1
    return 0 if response['ok'] else 1


def main():return run(sys.stdin.buffer,sys.stdout.buffer)


if __name__=='__main__':raise SystemExit(main())
