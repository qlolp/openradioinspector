"""Verify a Debian candidate's installed WebUI and shared synthetic archive fixture."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import quote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.release.smoke_macos_personal_rc import request, verify_original


def check_web_notices(documentation, manifest):
    notices = documentation / 'notices'
    inventory = (notices / 'licenses.json').read_bytes()
    if hashlib.sha256(inventory).hexdigest() != manifest['license_manifest_sha256']:
        raise ValueError('Installed license inventory differs from the release manifest')
    packages = {entry['name']: entry for entry in json.loads(inventory)['npm']}
    locked = json.loads((notices / 'package-lock.json').read_text())['packages']
    for name, files in [('vite', ['LICENSE.md']), ('rolldown', ['LICENSE', 'THIRD-PARTY-LICENSE'])]:
        entry = packages[name]
        dependency = locked['node_modules/' + name]
        if any(entry[key] != dependency[key] for key in ('version', 'integrity')):
            raise ValueError(f'Installed {name} notice differs from the WebUI lock')
        for filename in files:
            relative = f'npm/{name}/{filename}'
            if relative not in entry['notice_files'] or not (notices / relative).read_bytes().strip():
                raise ValueError(f'Missing embedded WebUI notice: {relative}')


def check_web(url, manifest):
    caps = request(url, 'GET', '/api/v1/capabilities')
    if caps['candidate']['mode'] != 'live' or not caps['persistent_archive'] or caps['features']['analog_audio_inputs']:
        raise ValueError('Unexpected installed core mode or archive')
    health = request(url, 'GET', '/api/v1/health')
    if health.get('status') != 'ok' or health.get('backend') != 'rust-candidate':
        raise ValueError('Rust core failed health probe')
    assets = dict(manifest['web_assets'])
    assets['/modern/'] = assets['/modern/index.html']
    for path, expected in assets.items():
        with urlopen(url + path, timeout=10) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f'Installed SPA differs from Vite output at {path}')
    return health


def exercise(url, phase):
    fixture = json.loads((ROOT / 'contracts/fixtures/measurement-v1.json').read_text(encoding='utf-8'))
    route = '/api/v1/sessions/' + quote(fixture['session']['id'], safe='')
    if phase == 'install':
        request(url, 'POST', '/api/v1/sessions', fixture['session'])
        request(url, 'POST', route + '/start', {})
        request(url, 'POST', route + '/sweeps', fixture['sweep'])
    if phase == 'upgrade':
        request(url, 'POST', route + '/observations', fixture['observation'])
    rollback = dict(fixture['observation'], id='during-package-rollback', level=96.0)
    if phase == 'rollback':
        request(url, 'POST', route + '/observations', rollback)
    resources = [('sweeps', 'sweep')]
    if phase != 'install':
        resources.append(('observations', 'observation'))
    for plural, key in resources:
        rows = request(url, 'GET', route + '/' + plural + '?limit=100')
        expected = [fixture[key]]
        if key == 'observation' and phase in ('rollback', 'restart', 'existing-v6'):
            expected.append(rollback)
        by_id = {row['id']: row for row in rows}
        if len(rows) != len(expected) or set(by_id) != {item['id'] for item in expected}:
            raise ValueError(f'{phase} lost or duplicated {plural}')
        for item in expected:
            verify_original(by_id[item['id']], item, key)
    if any(worker['alive'] for worker in request(url, 'GET', '/api/v1/workers')):
        raise ValueError('Fixture unexpectedly started hardware')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('inspect', 'clean-install', 'install', 'upgrade', 'rollback', 'restart', 'existing-v6'))
    parser.add_argument('--url', default='http://127.0.0.1:8770')
    parser.add_argument('--manifest', type=Path, default=Path('/usr/share/doc/openradioinspector-pi-rc/release.json'))
    args = parser.parse_args()
    info = json.loads(args.manifest.read_text(encoding='utf-8'))
    if args.phase == 'clean-install':
        check_web_notices(args.manifest.parent, info)
    check_web(args.url, info)
    if args.phase not in ('inspect', 'clean-install'):
        exercise(args.url, args.phase)
    print(json.dumps({'phase': args.phase, 'source_revision': info['source_revision'], 'debian_version': info['debian_version'],
                      'web_assets_checked': len(info['web_assets']), 'result': 'passed'}))
