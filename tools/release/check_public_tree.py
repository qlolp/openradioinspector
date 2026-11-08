#!/usr/bin/env python3
"""Reject known research blobs and proprietary asset paths; not a legal opinion."""
import argparse
import hashlib
import tarfile
import zipfile
import subprocess
from pathlib import Path

FORBIDDEN = ('CD Ligtht 32314', 'docs/commercial-help-texts.md')


def violations(repository, blocked, history=False, revision='HEAD'):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repository), '-c', 'core.quotePath=false', *args])
    bad = []
    if history:
        for line in git('rev-list', '--objects', '--all').decode().splitlines():
            oid, _, path = line.partition(' ')
            if oid in blocked or any(path == p or path.startswith(p+'/') for p in FORBIDDEN):
                bad.append((oid, path))
    else:
        for item in git('ls-tree', '-rz', revision).split(b'\0'):
            if not item:
                continue
            metadata, path = item.decode().split('\t', 1)
            _, kind, oid = metadata.split()
            if kind == 'blob' and (oid in blocked or any(path == p or path.startswith(p+'/') for p in FORBIDDEN)):
                bad.append((oid, path))
    return bad


def artifact_violations(path, blocked):
    bad = []
    def check(name, data):
        oid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        normalized = "/" + name.strip("/")
        if oid in blocked or any(normalized.endswith("/" + p) or "/" + p + "/" in normalized for p in FORBIDDEN):
            bad.append((oid, name))
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    check(member.filename, archive.read(member))
    else:
        with tarfile.open(path) as archive:
            for member in archive:
                if member.isfile():
                    check(member.name, archive.extractfile(member).read())
    return bad


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository', type=Path, default=Path('.'))
    p.add_argument('--blocked-blobs', type=Path, default=Path(__file__).with_name('blocked-blobs.txt'))
    p.add_argument('--history', action='store_true')
    p.add_argument('--artifact', type=Path)
    p.add_argument('--directory', type=Path, help='Scan an unpacked desktop package')
    p.add_argument('--revision', default='HEAD')
    args = p.parse_args()
    blocked = set(args.blocked_blobs.read_text().splitlines())
    if args.directory:
        bad = []
        for path in args.directory.rglob('*'):
            if path.is_file():
                data = path.read_bytes()
                oid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                name = path.relative_to(args.directory).as_posix()
                if oid in blocked or any(name == p or name.startswith(p + '/') for p in FORBIDDEN):
                    bad.append((oid, name))
    else:
        bad = artifact_violations(args.artifact, blocked) if args.artifact else violations(args.repository, blocked, args.history, args.revision)
    for oid, path in bad:
        print(f'Forbidden asset: {oid} {path}')
    if bad:
        return 1
    print('No known proprietary assets found in ' + (str(args.artifact) if args.artifact else ('reachable history' if args.history else args.revision)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
