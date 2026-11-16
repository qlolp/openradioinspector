"""Bounded local evidence and exact package identity; runtime artifacts stay outside Git."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import uuid
from tools.modernization.api_parity import load_json
from tools.release import macos_rc_package as package
from ..config import BenchmarkError


def read_json_lines(path,maximum=256*1024*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>maximum:
        raise BenchmarkError('invalid_evidence','Missing, linked or oversized evidence file')
    total=0
    with path.open('rb') as stream:
        while raw:=stream.readline(65537):
            total+=len(raw)
            if total>maximum or len(raw)>65536 or not raw.endswith(b'\n'):
                raise BenchmarkError('invalid_evidence','Truncated or oversized trace line')
            try:value=load_json(raw,65536)
            except (ValueError,TypeError,UnicodeError) as error:
                raise BenchmarkError('invalid_evidence','Strict trace JSON required') from error
            if not isinstance(value,dict):raise BenchmarkError('invalid_evidence','Trace row must be an object')
            yield value


def private_json(path,value):
    path=Path(path)
    raw=json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode()+b'\n'
    if len(raw)>65536:raise BenchmarkError('invalid_config','Private profile exceeds64KiB')
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.pending')
    descriptor=None
    try:
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(descriptor,'wb') as stream:
            descriptor=None;stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.link(temporary,path)
    except FileExistsError as error:raise BenchmarkError('existing_evidence','Refusing to overwrite an existing request/profile') from error
    finally:
        if descriptor is not None:os.close(descriptor)
        temporary.unlink(missing_ok=True)


def verify_host(value):
    return value.get('system')=='Darwin' and value.get('architecture')=='arm64' \
        and re.fullmatch(r'27\.3(?:\.\d+)*',str(value.get('macos_version',''))) is not None \
        and type(value.get('memory_bytes')) is int and value['memory_bytes']>=16*1024**3


def bundle_identity(app,manifest):
    app=Path(app).resolve(strict=True);manifest=Path(manifest).resolve(strict=True)
    if manifest.is_relative_to(app):raise BenchmarkError('invalid_bundle','Manifest must be outside the signed bundle')
    value=package.read_json(manifest,32*1024*1024)
    package.validate_runtime_profile(app);package.verify_inventory(app,value['files'])
    build=value['build_info'];source=value['source_revision']
    if build['source_revision']!=source or not re.fullmatch('[0-9a-f]{40}',source):
        raise BenchmarkError('invalid_bundle','Package source identity differs')
    package.validate_components(build['components']['ori-core'],build['components']['ori-supervisor'],source)
    return dict(source_revision=source,core_sha256=package.digest(app/'Contents/Helpers/ori-core'),
                native_sha256=package.digest(app/('Contents/MacOS/'+package.UI_EXECUTABLE)),
                supervisor_sha256=package.digest(app/'Contents/Helpers/ori-supervisor'),
                manifest_sha256=package.digest(manifest),app=str(app))
