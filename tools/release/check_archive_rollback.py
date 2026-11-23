#!/usr/bin/env python3
"""Exercise distinct previous/current core binaries against one synthetic installed-node layout."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import quote

if __package__:
    from .smoke_macos_personal_rc import core, request, verify_original
else:
    from smoke_macos_personal_rc import core, request, verify_original

ROOT = Path(__file__).resolve().parents[2]


def check(previous, current, supervisor, volume_parent=None):
    identities = [json.loads(subprocess.check_output([exe, '--build-info'])) for exe in (previous, current)]
    revisions = [identity['source_revision'] for identity in identities]
    if not all(revisions) or revisions[0] == revisions[1]:
        raise ValueError('Two distinct identified core revisions are required')
    controller = json.loads(subprocess.check_output([supervisor, '--build-info']))
    if controller['component'] != 'ori-supervisor' or controller['source_revision'] != revisions[1]:
        raise ValueError('The current core and supervisor must identify the same revision')
    fixture = json.loads((ROOT / 'contracts/fixtures/measurement-v1.json').read_text(encoding='utf-8'))
    observations = [dict(fixture['observation'], id=label, level=level)
                    for label, level in [('before-move', 137.0), ('after-move', 41.0), ('during-rollback', 96.0)]]
    route = '/api/v1/sessions/' + quote(fixture['session']['id'], safe='')
    env = {key: value for key, value in os.environ.items() if not key.startswith('ORI_RUNTIME_')}

    def read(url, expected):
        rows = request(url, 'GET', route + '/observations?limit=100')
        originals = {row['id']: row for row in rows}
        if len(rows) != len(expected) or set(originals) != {record['id'] for record in expected}:
            raise AssertionError('Committed observation IDs differ across the version change')
        for record in expected:
            verify_original(originals[record['id']], record, 'observation')

    def refused(args):
        result = subprocess.run(args, input=b'', capture_output=True, timeout=30, env=env)
        if result.returncode == 0:
            raise AssertionError('An unsafe archive operation succeeded')

    with tempfile.TemporaryDirectory() as node_root, tempfile.TemporaryDirectory(dir=volume_parent) as volume:
        node = Path(node_root).resolve()
        external = Path(volume).resolve() / 'measurements'
        subprocess.run([previous, 'archive-init-compact', node / 'sessions.sqlite3'], check=True, stdout=subprocess.DEVNULL)
        with core(previous, node, env) as url:
            request(url, 'POST', '/api/v1/sessions', fixture['session'])
            request(url, 'POST', route + '/start', {})
            request(url, 'POST', route + '/observations', observations[0])
        subprocess.run([supervisor, 'archive-location', '--data-dir', node, '--directory', external],
                       check=True, stdout=subprocess.DEVNULL)
        with core(current, node, env) as url:
            read(url, observations[:1])
            request(url, 'POST', route + '/observations', observations[1])
        startup = ['--mode', 'live', '--data-dir', node, '--port', '0', '--stdin-lifeline']
        refused([previous, *startup])
        external.rename(external.with_name('detached'))
        handoff = [supervisor, 'archive-prepare-rollback', '--data-dir', node]
        for command in ([previous, *startup], [current, *startup], handoff):
            refused(command)
        if external.exists():
            raise AssertionError('An unavailable storage directory was recreated')
        external.with_name('detached').rename(external)
        subprocess.run(handoff, check=True, stdout=subprocess.DEVNULL)
        with core(previous, node, env) as url:
            read(url, observations[:2])
            request(url, 'POST', route + '/observations', observations[2])
        with core(current, node, env) as url:
            read(url, observations)
        return dict(previous=revisions[0], current=revisions[1], observations=[r['id'] for r in observations],
                    distinct_filesystems=node.stat().st_dev != external.stat().st_dev)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('previous_core', 'current_core', 'current_supervisor'):
        parser.add_argument(name, type=lambda p: Path(p).resolve(strict=True))
    parser.add_argument('--volume-parent', type=Path, help='Existing private test volume parent')
    args = parser.parse_args()
    print(json.dumps(check(args.previous_core, args.current_core, args.current_supervisor, args.volume_parent)))
