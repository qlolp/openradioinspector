#!/usr/bin/env python3
"""Rehearse in a NEW private mirror and compare every retained file/mode.

Never pushes, deletes original refs, or establishes an off-machine backup.
Run after fetching a fresh complete snapshot, then archive the commit/ref maps.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

from check_public_tree import FORBIDDEN


def run(repository, *args):
    return subprocess.check_output(['git', '-C', str(repository), *args])


def refs(repository):
    return dict(line.split(' ', 1) for line in run(repository, 'for-each-ref', '--format=%(refname) %(objectname)').decode().splitlines())


def tree(repository, revision, blocked, *, omit=False):
    result = {}
    for item in run(repository, 'ls-tree', '-rz', revision).split(b'\0'):
        if not item:
            continue
        metadata, raw_path = item.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        path = raw_path.decode('utf-8', errors='surrogateescape')
        if omit and (oid in blocked or any(path == p or path.startswith(p+'/') for p in FORBIDDEN)):
            continue
        result[path] = (mode, kind, oid)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository', type=Path)
    parser.add_argument('output', type=Path, help='Must not exist; use private storage')
    parser.add_argument('--blocked-blobs', type=Path, default=Path(__file__).with_name('blocked-blobs.txt'))
    args = parser.parse_args()
    repository = args.repository.resolve()
    output = args.output.resolve()
    if output.exists() or output == repository or repository in output.parents:
        parser.error('Use a new private directory outside the source repository')
    blocked = set(args.blocked_blobs.read_text().splitlines())
    if 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391' in blocked:
        parser.error('The common empty blob must never be globally stripped')
    executable = str(Path(sys.executable).with_name('git-filter-repo'))
    if not Path(executable).is_file():
        executable = shutil.which('git-filter-repo')
    if not executable:
        parser.error('Install the reviewed git-filter-repo version before rehearsal')
    old_refs = refs(repository)
    output.mkdir(mode=0o700)
    mirror = output/'clean.git'
    subprocess.run(['git', 'clone', '--mirror', '--no-hardlinks', str(repository), str(mirror)], check=True)
    command = [executable, '--force', '--strip-blobs-with-ids', str(args.blocked_blobs.resolve())]
    for path in FORBIDDEN:
        command += ['--path', path]
    command += ['--invert-paths', '--refs', *old_refs]
    subprocess.run(command, cwd=mirror, check=True)
    new_refs = refs(mirror)
    if old_refs.keys() != new_refs.keys():
        raise RuntimeError('Retained refs changed; inspect before any server operation')
    lines = ['ref\told_sha\tnew_sha\n']
    for ref, old in sorted(old_refs.items()):
        expected = tree(repository, old, blocked, omit=True)
        actual = tree(mirror, new_refs[ref], blocked)
        if actual != expected:
            changed = sorted(path for path in set(expected)|set(actual) if expected.get(path) != actual.get(path))
            raise RuntimeError(f'Own paths/modes/content changed at {ref}: {changed[:20]!r}')
        lines.append(f'{ref}\t{old}\t{new_refs[ref]}\n')
    (output/'ref-map.tsv').write_text(''.join(lines),encoding='utf-8')
    shutil.copyfile(mirror/'filter-repo/commit-map', output/'commit-map.tsv')
    # A partial/ref-explicit rewrite keeps unreachable old objects. Purge only
    # this verified new mirror, never the source or its backup.
    subprocess.run(['git', '-C', str(mirror), 'config', '--local', '--remove-section', 'remote.origin'], check=True)
    run(mirror, 'reflog', 'expire', '--expire=now', '--all')
    run(mirror, 'gc', '--prune=now')
    print(f'Verified {len(old_refs)} refs: all retained paths, modes and contents match, including empty files.')
    print('Local rehearsal only. External backup and server freshness are separate gates.')


if __name__ == '__main__':
    main()
