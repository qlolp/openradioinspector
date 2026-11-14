"""Sealed recovery snapshots and explicitly derived immutable record sources."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import struct

from .protocol import I64_MAX, WorkerError

SCHEMA_VERSION = 6
COLUMNS = {
    'sessions': {'id'},
    'streams': {'id','session_id','device','role','level_unit','label','settings_json','capabilities_json'},
    'sweeps': {'id','session_id','stream_id','started_at','ended_at','frequencies_json',
               'levels_json','level_unit','settings_json'},
    'observations': {'id','session_id','stream_id','started_at','ended_at','frequency_hz',
                     'mode','level','level_unit','squelch_open','decoded_mode','audio_uri','metadata_json'},
    'events': {'sequence'},
}


def canonical_extensions(db,tables):
    marker,bits='ori_archive_schema','ori_observation_level_bits'
    if marker not in tables and bits not in tables:
        return {}
    if marker not in tables or bits not in tables:
        _invalid()
    for name,columns in ((marker,{'singleton','version'}),(bits,{'observation_id','level_bits_le'})):
        if not columns <= {row[1] for row in db.execute(f'PRAGMA table_info({name})')}:
            _invalid()
    rows=[tuple(row) for row in db.execute('SELECT singleton,version FROM ori_archive_schema').fetchmany(2)]
    if len(rows)!=1 or rows[0][0]!=1:
        _invalid()
    if type(rows[0][1]) is not int or rows[0][1]!=1:
        raise WorkerError('unsupported_schema','Unsupported canonical observation precision extension')
    return {'observation_level_bits':1}


def observation_level(stored,bits):
    if bits is None:
        return stored
    if not isinstance(bits,bytes) or len(bits)!=8 or stored is None:
        raise WorkerError('source_record_invalid','Invalid canonical observation level bytes')
    value=struct.unpack('<d',bits)[0]
    # Numeric equality intentionally accepts the two zero signs. The original
    # finite bits are authoritative only when they agree with stored REAL.
    if not math.isfinite(value) or value!=stored:
        raise WorkerError('source_record_invalid','Canonical observation bits disagree with stored REAL')
    return value


def observation_rows(db,scope,parameters):
    columns=('id','session_id','stream_id','started_at','ended_at','frequency_hz','mode',
             'level','level_unit','squelch_open','decoded_mode','audio_uri','metadata_json')
    precision=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ori_observation_level_bits'").fetchone()
    fields=','.join('observation.'+column for column in columns)
    fields+=',precision.level_bits_le' if precision else ',NULL AS level_bits_le'
    source=' FROM observations AS observation'
    if precision:
        source+=' LEFT JOIN ori_observation_level_bits AS precision ON precision.observation_id=observation.id'
    qualified=scope.replace('session_id=?','observation.session_id=?').replace('stream_id=?','observation.stream_id=?')
    return db.execute('SELECT '+fields+source+qualified+' ORDER BY observation.id',parameters)


def file_digest(path, budget, *, source=False):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(65_536):
            if source:
                budget.account_io(len(block), 'source_bytes_read')
            else:
                budget.account_io(len(block), 'cache_bytes_read')
            result.update(block)
    return result.hexdigest()


def _invalid():
    raise WorkerError('snapshot_invalid', 'Sealed recovery snapshot verification failed')


def _unsealed(path):
    if any(p.exists() or p.is_symlink() for p in
           (Path(str(path)+suffix) for suffix in ('-wal','-shm','-journal'))):
        raise WorkerError('unsealed_snapshot', 'Snapshot has live SQLite sidecars')


def _file(path):
    if path.is_symlink() or not path.is_file():
        _invalid()


@contextmanager
def open_snapshot(path, filters, budget):
    path = Path(path)
    if path.name == 'records.ndjson':
        from .derived import open_derived
        with open_derived(path,filters,budget) as source:
            yield source
        return
    _file(path)
    path = path.resolve(strict=True)
    _unsealed(path)
    metadata = path.parent / 'manifest.json'
    artifacts = path.parent / 'artifacts.ndjson'
    _file(metadata)
    _file(artifacts)
    if metadata.stat().st_size > 65_536:
        _invalid()
    raw = metadata.read_bytes()
    budget.account_io(len(raw), 'source_bytes_read')
    try:
        manifest = json.loads(raw)
        if manifest['backup_version'] != 1 or manifest['purpose'] != 'private-recovery' \
                or manifest['archive']['file'] != path.name or path.name != 'archive.sqlite' \
                or manifest['artifacts']['file'] != 'artifacts.ndjson' \
                or type(manifest['event_watermark']) is not int \
                or manifest['event_watermark'] < 0:
            _invalid()
        if type(manifest['archive_schema']) is not int or manifest['archive_schema'] != SCHEMA_VERSION:
            raise WorkerError('unsupported_schema', 'Research supports archive schema 6 only')
        signature = path.stat()
        sha = file_digest(path, budget, source=True)
        if sha != manifest['archive']['sha256'] or signature.st_size != manifest['archive']['bytes'] \
                or file_digest(artifacts, budget, source=True) != manifest['artifacts']['sha256']:
            _invalid()
        db = sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, budget.limits['max_record_bytes'])
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA cache_size=-2048')
            db.execute('PRAGMA mmap_size=0')
            if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise WorkerError('unsupported_schema', 'Research supports archive schema 6 only')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not COLUMNS.keys() <= tables:
                _invalid()
            for table, expected in COLUMNS.items():
                if not expected <= {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
                    _invalid()
            extensions=canonical_extensions(db,tables)
            if db.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok' \
                    or db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                _invalid()
            counts = {name:db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                      for name in ('sweeps','observations','events')}
            counts['artifacts'] = (db.execute('SELECT COUNT(*) FROM instrument_artifacts').fetchone()[0]
                                    if 'instrument_artifacts' in tables else 0)
            watermark = db.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]
            if counts != manifest['counts'] or watermark != manifest['event_watermark']:
                _invalid()
            if 'session_id' in filters and not db.execute('SELECT 1 FROM sessions WHERE id=?',
                                                          (filters['session_id'],)).fetchone():
                raise WorkerError('invalid_filter', 'Session filter is absent from the snapshot')
            if 'stream_id' in filters:
                stream = db.execute('SELECT session_id FROM streams WHERE id=?',
                                    (filters['stream_id'],)).fetchone()
                if stream is None or ('session_id' in filters and stream[0] != filters['session_id']):
                    raise WorkerError('invalid_filter', 'Stream filter is absent from the selected session')
            source = {'sha256':sha, 'bytes':signature.st_size, 'archive_schema':SCHEMA_VERSION,
                'canonical_extensions':extensions,
                'event_watermark':watermark, 'counts':counts,
                'recovery_manifest_sha256':hashlib.sha256(raw).hexdigest(),
                'artifact_manifest_sha256':manifest['artifacts']['sha256']}
            yield db, source
            current = path.stat()
            if (signature.st_dev,signature.st_ino,signature.st_size,signature.st_mtime_ns) != \
                    (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns):
                _invalid()
            _unsealed(path)
        finally:
            db.close()
    except sqlite3.DataError as error:
        if getattr(error,'sqlite_errorcode',None) == sqlite3.SQLITE_TOOBIG:
            raise WorkerError('source_record_too_large', 'Source record exceeds the SQLite read limit') from None
        _invalid()
    except (OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError, OverflowError, RecursionError):
        _invalid()


def text(value, maximum=1024, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value,str) or len(value.encode('utf-8')) > maximum:
        raise WorkerError('source_record_invalid', 'Invalid source text field')
    return value


def _json_document(value, maximum):
    text(value, maximum)
    def invalid(_):
        raise ValueError()
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError()
            result[key] = item
        return result
    def finite(raw):
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError()
        return number
    try:
        return json.loads(value, parse_constant=invalid, parse_float=finite,
                          object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise WorkerError('source_record_invalid', 'Source settings or metadata are invalid') from None


def json_object(value, maximum):
    if not isinstance(_json_document(value, maximum), dict):
        raise WorkerError('source_record_invalid', 'Source settings or metadata are invalid')
    return value


def json_capabilities(value, maximum):
    parsed = _json_document(value, maximum)
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise WorkerError('source_record_invalid', 'Source capabilities must be a JSON string array')
    return value


def timestamp(value):
    try:
        text(value,128)
        if any(len(part) > 6 for part in re.findall(r'[.,]([0-9]+)',value)):
            raise ValueError()
        instant = datetime.fromisoformat(value.replace('Z','+00:00'))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError()
        delta = instant.astimezone(timezone.utc) - datetime(1970,1,1,tzinfo=timezone.utc)
        return delta.days*86_400_000_000 + delta.seconds*1_000_000 + delta.microseconds
    except (ValueError, TypeError, OverflowError):
        raise WorkerError('source_record_invalid', 'Source time must have a UTC offset') from None


def interval(row):
    start,end = timestamp(row['started_at']),timestamp(row['ended_at'])
    if start > end:
        raise WorkerError('source_record_invalid', 'Invalid source measurement interval')
    return start,end


def frequency(value):
    if type(value) is not int or not 0 <= value <= I64_MAX:
        raise WorkerError('source_record_invalid', 'Source frequency must be integer Hz')
    return value


def scope_sql(filters):
    clauses, parameters = [],[]
    for key in ('session_id','stream_id'):
        if key in filters:
            clauses.append(f'{key}=?')
            parameters.append(filters[key])
    return (' WHERE '+' AND '.join(clauses) if clauses else ''),parameters


def time_selected(start, filters):
    return filters.get('started_at_us',-(2**63)) <= start < filters.get('ended_at_us',I64_MAX)


def frequency_selected(hz, filters):
    return filters.get('min_frequency_hz',0) <= hz <= filters.get('max_frequency_hz',I64_MAX)
