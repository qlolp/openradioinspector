"""Immutable selected original records; this is not a recovery backup format."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re

from .protocol import I64_MAX, WorkerError
from .source import _file, _invalid, _json_document, _unsealed, file_digest, text

KINDS = {'stream':('streams',0),'sweep':('sweeps',1),'observation':('observations',2)}
MANIFEST_KEYS = {'derived_source_version','purpose','records','event_watermark','filters',
                 'counts','audio_payloads_copied'}


class DerivedRecords:
    def __init__(self, path, manifest, budget):
        self.path,self.manifest,self.budget = path,manifest,budget
        self.validated = False
        self.digest_checked = False

    def records(self):
        counts = dict(streams=0,sweeps=0,observations=0)
        previous = None
        maximum = self.budget.limits['max_record_bytes']
        digest = hashlib.sha256()
        self.validated = self.digest_checked = False
        with self.path.open('rb') as source:
            while raw := source.readline(maximum+1):
                self.budget.account_io(len(raw),'source_bytes_read')
                digest.update(raw)
                if len(raw)>maximum:
                    raise WorkerError('source_record_too_large','Derived source record exceeds the read budget')
                try:
                    if not raw.endswith(b'\n'):
                        raise ValueError()
                    record = _json_document(raw.decode('utf-8'),maximum)
                    if not isinstance(record,dict) or set(record)!={'kind','value'} \
                            or record['kind'] not in KINDS or not isinstance(record['value'],dict):
                        raise ValueError()
                    name,phase = KINDS[record['kind']]
                    value = record['value']
                    selection = self.manifest['filters']
                    if 'session_id' in selection and value['session_id']!=selection['session_id']:
                        raise ValueError()
                    if 'stream_id' in selection and value['id' if record['kind']=='stream' else 'stream_id']!=selection['stream_id']:
                        raise ValueError()
                    identifier = text(value['id']).encode('utf-8')
                    order = (phase,identifier)
                    if previous is not None and order<=previous:
                        raise ValueError()
                except (ValueError,KeyError,TypeError,UnicodeError,RecursionError):
                    raise WorkerError('source_record_invalid','Invalid or unordered derived source record') from None
                previous = order
                counts[name] += 1
                yield record['kind'],record['value']
        self.digest_checked = True
        if digest.hexdigest()!=self.manifest['records']['sha256']:
            _invalid()
        if counts!=self.manifest['counts']:
            _invalid()
        self.validated = True


@contextmanager
def open_derived(path, filters, budget):
    path = Path(path)
    _file(path)
    path = path.resolve(strict=True)
    _unsealed(path)
    metadata = path.parent/'manifest.json'
    _file(metadata)
    if metadata.stat().st_size>65536:
        _invalid()
    raw = metadata.read_bytes()
    budget.account_io(len(raw),'source_bytes_read')
    try:
        manifest = _json_document(raw.decode('utf-8'),65536)
        if not isinstance(manifest,dict) or set(manifest)!=MANIFEST_KEYS \
                or type(manifest['derived_source_version']) is not int or manifest['derived_source_version']!=1 \
                or manifest['purpose']!='derived-history' or manifest['audio_payloads_copied'] is not False \
                or path.name!='records.ndjson' \
                or type(manifest['event_watermark']) is not int or not 0<=manifest['event_watermark']<=I64_MAX:
            _invalid()
        entry = manifest['records']
        counts = manifest['counts']
        if not isinstance(entry,dict) or set(entry)!={'file','bytes','sha256'} \
                or entry['file']!=path.name or type(entry['bytes']) is not int or entry['bytes']<0 \
                or not isinstance(entry['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',entry['sha256']) \
                or not isinstance(counts,dict) or set(counts)!={'streams','sweeps','observations'} \
                or any(type(value) is not int or not 0<=value<=I64_MAX for value in counts.values()):
            _invalid()
        if not isinstance(manifest['filters'],dict) or manifest['filters']!=filters:
            raise WorkerError('invalid_filter','Filters do not match the sealed derived selection')
        signature = path.stat()
        metadata_signature = metadata.stat()
        if signature.st_size!=entry['bytes']:
            _invalid()
        records = DerivedRecords(path,manifest,budget)
        source = dict(kind='scoped-derived-v1',derived=True,sha256=entry['sha256'],bytes=entry['bytes'],
            event_watermark=manifest['event_watermark'],counts=counts,selection=filters,
            source_manifest_sha256=hashlib.sha256(raw).hexdigest(),audio_payloads_copied=False)
        try:
            yield records,source
            # A cache hit must also finish the same digest/envelope/count pass.
            # The caller cannot publish or query until this context returns.
            if not records.validated:
                for _ in records.records():
                    pass
        except Exception:
            # Preserve the original digest-error precedence when a consumer
            # stops early. Failed paths still pay for the whole-file check.
            if not records.digest_checked and file_digest(path,budget,source=True)!=entry['sha256']:
                _invalid()
            raise
        for file,original in ((path,signature),(metadata,metadata_signature)):
            _file(file)
            current = file.stat()
            if (original.st_dev,original.st_ino,original.st_size,original.st_mtime_ns) != \
                    (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns):
                _invalid()
        _unsealed(path)
    except (OSError,ValueError,KeyError,TypeError,UnicodeError,OverflowError,RecursionError):
        _invalid()
