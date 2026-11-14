"""Only this worker's disposable cache is writable or reclaimable."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

import duckdb
import numpy
import pyarrow
import pyarrow.parquet as pq

from . import __version__
from .export import SCHEMAS, build_tables
from .protocol import WorkerError, encode, fingerprint
from .resources import QuotaFile
from .source import file_digest

OWNER = b'{"owner":"ori-research","version":1}\n'
FORMAT_VERSION = 1
SOFTWARE = {'worker':__version__, 'python':'.'.join(map(str,sys.version_info[:3])),
            'numpy':numpy.__version__, 'duckdb':duckdb.__version__, 'pyarrow':pyarrow.__version__}
OWNED_DIRECTORY = re.compile(r'(?:cache-[0-9a-f]{64}|\.stage-[0-9a-f]{32}|\.temp-[0-9a-f]{32})\Z')


def sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path,os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def tree_bytes(root, *, skip_temporary=True):
    size = 0
    for entry in root.iterdir():
        if entry.is_symlink():
            raise WorkerError('unsafe_cache', 'Cache contains a linked entry')
        if skip_temporary and (entry.name.startswith('.temp-') or entry.name == '.temporary'):
            continue
        if entry.is_dir():
            size += tree_bytes(entry,skip_temporary=False)
        elif entry.is_file():
            size += entry.stat().st_size
        else:
            raise WorkerError('unsafe_cache', 'Cache contains a special filesystem entry')
    return size


def prepare_root(request, budget):
    root = Path(request['cache_directory'])
    source_directory = Path(request['snapshot_path']).resolve().parent
    resolved = root.resolve()
    if root.is_symlink() or resolved == source_directory \
            or resolved.is_relative_to(source_directory) or source_directory.is_relative_to(resolved):
        raise WorkerError('unsafe_cache', 'Snapshot and cache directories must be separate')
    if root.exists() and not root.is_dir():
        raise WorkerError('unsafe_cache', 'Cache must be a dedicated directory')
    if root.exists() and any(root.iterdir()):
        marker = root / '.ori-research-owner'
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size != len(OWNER) \
                or marker.read_bytes() != OWNER:
            raise WorkerError('unsafe_cache', 'Existing directory is not an owned analytics cache')
        budget.cache_used = tree_bytes(root)
        if budget.cache_used > budget.limits['cache_bytes']:
            raise WorkerError('cache_quota_exceeded', 'Existing derived cache exceeds byte quota', retryable=True)
    else:
        if len(OWNER) > budget.limits['cache_bytes']:
            raise WorkerError('cache_quota_exceeded', 'Derived cache byte quota is too small')
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
        with QuotaFile(root/'.ori-research-owner',budget) as output:
            output.write(OWNER)
        sync_directory(root)
    return root.resolve()


def remove_owned(directory, root):
    if directory.parent != root or not OWNED_DIRECTORY.fullmatch(directory.name) or directory.is_symlink():
        raise WorkerError('unsafe_cache', 'Refusing to remove an unowned cache path')
    shutil.rmtree(directory)


def remove_abandoned(root, budget):
    for entry in root.iterdir():
        if OWNED_DIRECTORY.fullmatch(entry.name) and entry.name.startswith(('.stage-','.temp-')):
            remove_owned(entry,root)
        if entry.name == '.temporary':
            if not entry.is_dir() or entry.is_symlink():
                raise WorkerError('unsafe_cache', 'Temporary cache path is not an owned directory')
            for temporary in entry.iterdir():
                if OWNED_DIRECTORY.fullmatch(temporary.name) and temporary.name.startswith('.temp-'):
                    remove_owned(temporary,entry)
    budget.cache_used = tree_bytes(root)


def identity(source, filters):
    return fingerprint({'format_version':FORMAT_VERSION,'software':SOFTWARE,
                        'source':source,'filters':filters})


def verify_cache(directory, key, source, filters, budget):
    """No cache hit is accepted without matching context and every file digest."""
    try:
        marker = directory/'manifest.json'
        if directory.is_symlink() or not directory.is_dir() or marker.is_symlink() \
                or not marker.is_file() or marker.stat().st_size > 65_536:
            return None
        raw = marker.read_bytes()
        manifest = json.loads(raw)
        if manifest['format_version'] != FORMAT_VERSION or manifest['cache_key'] != key \
                or manifest['source'] != source or manifest['filters'] != filters \
                or manifest['software'] != SOFTWARE or set(manifest['files']) != set(SCHEMAS):
            return None
        if set(p.name for p in directory.iterdir()) != {'manifest.json',*(name+'.parquet' for name in SCHEMAS)}:
            return None
        for name,schema in SCHEMAS.items():
            path = directory/(name+'.parquet')
            entry = manifest['files'][name]
            if path.is_symlink() or not path.is_file() or entry['file'] != path.name \
                    or path.stat().st_size != entry['bytes'] or file_digest(path,budget) != entry['sha256']:
                return None
            footer = pq.ParquetFile(path)
            if footer.metadata.num_rows != manifest['counts'][name] or not footer.schema_arrow.equals(schema):
                return None
        return manifest,hashlib.sha256(raw).hexdigest()
    except (OSError,ValueError,KeyError,TypeError,pyarrow.ArrowException):
        return None


def build_cache(db, root, key, source, filters, budget):
    stage = root/('.stage-'+uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        counts = build_tables(db,stage,filters,budget)
        files = {name:{'file':name+'.parquet','bytes':(stage/(name+'.parquet')).stat().st_size,
                       'sha256':file_digest(stage/(name+'.parquet'),budget)} for name in SCHEMAS}
        manifest = {'format_version':FORMAT_VERSION,'cache_key':key,'software':SOFTWARE,
                    'source':source,'filters':filters,'counts':counts,'files':files,
                    'derived':True,'audio_payloads_copied':False}
        raw = encode(manifest)+b'\n'
        with QuotaFile(stage/'manifest.json',budget) as output:
            output.write(raw)
        sync_directory(stage)
        return stage,manifest,hashlib.sha256(raw).hexdigest()
    except BaseException:
        remove_owned(stage,root)
        budget.cache_used = tree_bytes(root)
        raise


def publish(stage, destination):
    # The per-root OS lock excludes another worker. No valid cache name is
    # visible until writers, hashes, manifest and source checks have completed.
    if destination.exists() or destination.is_symlink():
        raise WorkerError('unsafe_cache', 'Cache publication destination already exists')
    os.rename(stage,destination)
    sync_directory(destination.parent)
