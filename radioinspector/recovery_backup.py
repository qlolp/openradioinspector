"""Private, verified recovery bundles. Portable sharing uses monitoring.archive_backup.

Recovery retains restart intent and private configuration. Nothing here starts
hardware or replays commands. Closed audio artifacts are already stored as BLOBs
in the canonical archive; their manifest verifies each original payload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

CHUNK_BYTES = 1024 * 1024
FILES = ('archive.sqlite', 'artifacts.ndjson', 'manifest.json')


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(CHUNK_BYTES), b''):
            digest.update(block)
    return digest.hexdigest()


def _readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)


def _integrity(db):
    if db.execute('PRAGMA integrity_check(1)').fetchone() != ('ok',):
        raise ValueError('Archive integrity check failed')
    if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('Archive foreign key check failed')
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'sessions', 'streams', 'sweeps', 'observations', 'events'} <= tables:
        raise ValueError('Not an OpenRadioInspector archive')
    return tables


def _artifacts(db, tables):
    if 'instrument_artifacts' not in tables:
        return
    cursor = db.execute('SELECT id,kind,mime_type,sha256,length(payload) FROM instrument_artifacts ORDER BY id')
    while True:
        rows = cursor.fetchmany(128)
        if not rows:
            break
        for identifier, kind, mime, expected, size in rows:
            digest = hashlib.sha256()
            for offset in range(0, size, CHUNK_BYTES):
                block = db.execute('SELECT substr(payload,?,?) FROM instrument_artifacts WHERE id=?',
                                   (offset+1, CHUNK_BYTES, identifier)).fetchone()[0]
                digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError('Closed artifact checksum mismatch')
            yield {'id': identifier, 'kind': kind, 'mime_type': mime, 'sha256': expected, 'bytes': size}


def _sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_json(path, value):
    with path.open('x', encoding='utf-8') as target:
        json.dump(value, target, ensure_ascii=False, allow_nan=False, indent=2)
        target.write('\n')
        target.flush()
        os.fsync(target.fileno())
    path.chmod(0o600)


def create_backup(source, destination):
    """Take a live WAL snapshot, verify it, then publish a new private bundle."""
    source = Path(source).expanduser().resolve(strict=True)
    destination = Path(destination).expanduser().absolute()
    if not source.is_file():
        raise ValueError('Archive source must be a file')
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ori-recovery-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        archive = staging / 'archive.sqlite'
        original = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        copied = sqlite3.connect(archive)
        try:
            original.execute('BEGIN')
            original.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
            original.backup(copied, pages=128)
            copied.execute('PRAGMA journal_mode=DELETE')
        finally:
            copied.close()
            original.close()
        archive.chmod(0o600)
        # Windows _commit/FlushFileBuffers requires a writable file handle.
        with archive.open('r+b') as handle:
            os.fsync(handle.fileno())
        db = _readonly(archive)
        try:
            tables = _integrity(db)
            count = 0
            with (staging/'artifacts.ndjson').open('x', encoding='utf-8') as target:
                for artifact in _artifacts(db, tables):
                    target.write(json.dumps(artifact, ensure_ascii=False, allow_nan=False) + '\n')
                    count += 1
                target.flush()
                os.fsync(target.fileno())
            (staging/'artifacts.ndjson').chmod(0o600)
            counts = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                      for table in ('sweeps', 'observations', 'events')}
            counts['artifacts'] = count
            manifest = {'backup_version': 1, 'purpose': 'private-recovery',
                'archive_schema': db.execute('PRAGMA user_version').fetchone()[0],
                'archive': {'file': 'archive.sqlite', 'sha256': _digest(archive),
                            'bytes': archive.stat().st_size},
                'artifacts': {'file': 'artifacts.ndjson', 'sha256': _digest(staging/'artifacts.ndjson')},
                'counts': counts,
                'event_watermark': db.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]}
        finally:
            db.close()
        _write_json(staging/'manifest.json', manifest)
        verify_backup(staging)
        # Reserve the destination exclusively. Manifest is the final commit
        # marker; interrupted bundles are never accepted as valid snapshots.
        destination.mkdir(mode=0o700)
        identity = destination.stat()
        try:
            for name in FILES:
                os.replace(staging/name, destination/name)
            _sync_directory(destination)
            _sync_directory(destination.parent)
        except BaseException:
            current = destination.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                shutil.rmtree(destination)
            raise
    return manifest


def verify_backup(directory):
    directory = Path(directory).expanduser().absolute()
    for name in FILES:
        if (directory/name).is_symlink() or not (directory/name).is_file():
            raise ValueError('Recovery bundle contains a missing or linked file')
    if (directory/'manifest.json').stat().st_size > 65_536:
        raise ValueError('Recovery manifest is too large')
    manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('backup_version') != 1:
        raise ValueError('Unsupported recovery manifest')
    try:
        if manifest['purpose'] != 'private-recovery' or manifest['archive']['file'] != 'archive.sqlite' \
                or manifest['artifacts']['file'] != 'artifacts.ndjson':
            raise ValueError('Unexpected recovery file path')
        for key, name in (('archive', 'archive.sqlite'), ('artifacts', 'artifacts.ndjson')):
            if _digest(directory/name) != manifest[key]['sha256']:
                raise ValueError('Recovery file checksum mismatch')
        if (directory/'archive.sqlite').stat().st_size != manifest['archive']['bytes']:
            raise ValueError('Archive size mismatch')
        if any((directory/('archive.sqlite'+suffix)).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise ValueError('Recovery bundle must be a sealed database')
        db = _readonly(directory/'archive.sqlite')
        try:
            tables = _integrity(db)
            count = 0
            with (directory/'artifacts.ndjson').open(encoding='utf-8') as entries:
                for artifact in _artifacts(db, tables):
                    line = entries.readline(CHUNK_BYTES)
                    if not line.endswith('\n') or json.loads(line) != artifact:
                        raise ValueError('Artifact manifest mismatch')
                    count += 1
                if entries.read(1):
                    raise ValueError('Unexpected artifact manifest entry')
            counts = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                      for table in ('sweeps', 'observations', 'events')}
            counts['artifacts'] = count
            watermark = db.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]
            if counts != manifest['counts'] or watermark != manifest['event_watermark'] \
                    or db.execute('PRAGMA user_version').fetchone()[0] != manifest['archive_schema']:
                raise ValueError('Recovery snapshot metadata mismatch')
        finally:
            db.close()
    except (KeyError, TypeError, sqlite3.DatabaseError, json.JSONDecodeError) as error:
        raise ValueError('Invalid recovery bundle') from error
    return manifest


def _check_restore_destination(destination):
    # SQLite will replay adjacent WAL/journal files even when the main file is
    # a verified snapshot. Never reuse a former database's sidecars.
    for candidate in (destination, *(Path(str(destination)+suffix)
                                      for suffix in ('-wal', '-shm', '-journal'))):
        if candidate.exists() or candidate.is_symlink():
            raise FileExistsError(candidate)


def restore_backup(directory, destination):
    """Verify and publish a new archive; the caller must stop its core first."""
    destination = Path(destination).expanduser().absolute()
    directory = Path(directory).expanduser().absolute()
    _check_restore_destination(destination)
    manifest = verify_backup(directory)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ori-restore-', dir=destination.parent) as temporary:
        staging = Path(temporary)/'archive.sqlite'
        with (directory/'archive.sqlite').open('rb') as source, staging.open('xb') as target:
            shutil.copyfileobj(source, target, CHUNK_BYTES)
            target.flush()
            os.fsync(target.fileno())
        staging.chmod(0o600)
        if _digest(staging) != manifest['archive']['sha256']:
            raise ValueError('Recovery source changed during restore')
        # Atomic, no-overwrite publication on the destination filesystem.
        # Unsupported hard links fail explicitly instead of exposing a partial DB.
        _check_restore_destination(destination)
        os.link(staging, destination)
        _sync_directory(destination.parent)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--source', required=True)
    create.add_argument('--output', required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--backup', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--backup', required=True)
    restore.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.command == 'create':
        result = create_backup(args.source, args.output)
    elif args.command == 'verify':
        result = verify_backup(args.backup)
    else:
        result = restore_backup(args.backup, args.output)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
