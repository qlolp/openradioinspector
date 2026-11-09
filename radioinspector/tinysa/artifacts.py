"""Immutable instrument artifacts; separate from unprocessed Sweep records."""
from __future__ import annotations
import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from ..contracts import parse_time
from .protocol import MAX_BINARY_BYTES


@dataclass
class InstrumentSample:
    interval: object
    artifact: dict
    id: str = field(default_factory=lambda:uuid.uuid4().hex)


def make_artifact(session_id,stream_id,value):
    if not isinstance(value,dict):raise ValueError('Artifact must be an object')
    kind=value.get('kind');mime=value.get('mime_type');name=value.get('filename')
    if kind not in {'screen','sd_file','raw_scan','zero_span','processed_trace','measurement','diagnostic','audio'}:
        raise ValueError('Unknown artifact kind')
    if mime not in {'image/png','application/octet-stream','application/json','text/csv','audio/wav'}:
        raise ValueError('Unsupported artifact MIME')
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',name) or '..' in name:
        raise ValueError('Invalid artifact filename')
    encoded=value.get('data_base64')
    if not isinstance(encoded,str) or len(encoded)>((MAX_BINARY_BYTES+2)//3)*4:raise ValueError('Artifact too large')
    try:raw=base64.b64decode(encoded,validate=True)
    except Exception as exc:raise ValueError('Invalid artifact encoding') from exc
    if len(raw)>MAX_BINARY_BYTES:raise ValueError('Artifact too large')
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=value.get('sha256'):raise ValueError('Artifact checksum mismatch')
    metadata=value.get('metadata')
    if not isinstance(metadata,dict):raise ValueError('Artifact metadata missing')
    serialized=json.dumps(metadata,ensure_ascii=False,allow_nan=False)
    if len(serialized.encode())>65536:raise ValueError('Artifact metadata too large')
    created=parse_time(metadata.get('occurred_at')).isoformat(timespec='microseconds').replace('+00:00','Z')
    return dict(id='instrument-'+uuid.uuid4().hex,session_id=session_id,stream_id=stream_id,
                schema_version=1,kind=kind,mime_type=mime,filename=name,sha256=digest,
                created_at=created,payload=raw,metadata=json.loads(serialized),size_bytes=len(raw))


def summary(value):
    return {k:v for k,v in value.items() if k!='payload'}
