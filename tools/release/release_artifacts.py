"""Freeze native package bytes and emit a small public build manifest."""
import argparse
import json
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from .macos_rc_package import digest, read_json, write_json


ARTIFACTS = {
    'macos': 'OpenRadioInspector-0.4.0-macos-arm64.dmg',
    'windows': 'OpenRadioInspector-0.4.0-windows-x64.exe',
    'pi': 'OpenRadioInspector-0.4.0-pi-arm64.deb',
}
TAG = {name: f'v0.4.0-{name}.rc.1' for name in ARTIFACTS}
COMPONENTS = {'ori-core', 'ori-supervisor', 'ui'}
TARGETS = {'macos': 'macos-arm64', 'windows': 'windows-x64', 'pi': 'raspberrypi-arm64'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(value, size=64):
    return isinstance(value, str) and bool(re.fullmatch(rf'[0-9a-f]{{{size}}}', value))


def keys(value, expected, label):
    require(isinstance(value, dict) and value.keys() == set(expected), f'incomplete or unexpected {label} fields')


def sums(directory, names):
    rows = []
    for name in sorted(names):
        path = directory / name
        require(path.is_file() and not path.is_symlink(), 'unexpected release asset type')
        rows.append(f'{digest(path)}  {name}\n')
    return ''.join(rows)


def write_sums(directory):
    names = {path.name for path in directory.iterdir()} - {'SHA256SUMS'}
    (directory / 'SHA256SUMS').write_bytes(sums(directory, names).encode('ascii'))


def verify_sums(directory, names):
    check = directory / 'SHA256SUMS'
    require({p.name for p in directory.iterdir()} == names | {'SHA256SUMS'}, 'unexpected or missing release assets')
    require(check.is_file() and not check.is_symlink() and check.stat().st_size < 64 * 1024,
            'invalid SHA256SUMS')
    require(check.read_bytes() == sums(directory, names).encode('ascii'), 'package checksums differ')


def signing_proof(platform, proof):
    if platform == 'macos':
        keys(proof, ['kind', 'certificate_sha1', 'team_id', 'hardened_runtime', 'timestamp',
                     'stapled', 'app_ticket', 'dmg_ticket'], 'Developer ID receipt')
        require(proof['kind'] == 'developer-id' and sha(proof['certificate_sha1'], 40)
                and re.fullmatch('[A-Z0-9]{10}', str(proof['team_id']))
                and all(proof[k] is True for k in ('hardened_runtime', 'timestamp', 'stapled')),
                'Developer ID signing, runtime, timestamp or stapling missing')
        for key in ('app_ticket', 'dmg_ticket'):
            require(str(uuid.UUID(proof[key])) == proof[key], 'invalid Apple notarization submission ID')
        require(proof['app_ticket'] != proof['dmg_ticket'], 'separate app and DMG acceptance required')
    elif platform == 'windows':
        keys(proof, ['kind', 'certificate_sha1', 'timestamp', 'verified'], 'Authenticode receipt')
        require(proof['kind'] == 'authenticode' and sha(proof['certificate_sha1'], 40)
                and proof['timestamp'] == 'RFC3161-SHA256'
                and proof['verified'] == ['ori-core', 'ori-supervisor', 'ui', 'nsis'],
                'incomplete timestamped Authenticode chain')
    else:
        require(proof == {'kind': 'debian-sha256'}, 'wrong Pi integrity mechanism')


def check_manifest(row, platform, revision, artifact):
    keys(row, ['schema_version', 'release_version', 'platform', 'target', 'source_revision', 'artifact', 'components',
               'manual_sha256', 'signing'], 'candidate manifest')
    require(type(row['schema_version']) is int and row['schema_version'] == 1 and row['release_version'] == '0.4.0' and row['platform'] == platform
            and row['target'] == TARGETS[platform] and row['source_revision'] == revision and sha(revision, 40),
            'wrong build revision, target or schema')
    keys(row['components'], COMPONENTS, 'native components')
    require(all(sha(v) for v in row['components'].values()) and sha(row['manual_sha256']), 'invalid component digest')
    keys(row['artifact'], ['name', 'bytes', 'sha256'], 'artifact')
    require(artifact.is_file() and not artifact.is_symlink() and row['artifact']['name'] == ARTIFACTS[platform]
            and type(row['artifact']['bytes']) is int and row['artifact']['bytes'] == artifact.stat().st_size > 0
            and row['artifact']['sha256'] == digest(artifact), 'artifact identity or size differs')
    signing_proof(platform, row['signing'])


def freeze(platform, source, evidence, artifact, output):
    require(not output.exists() and not output.is_symlink(), 'candidate output already exists')
    require(source['source_revision'] == evidence['source_revision'], 'mixed package evidence')
    record = dict(schema_version=1, release_version=source['release_version'], platform=platform, target=TARGETS[platform], source_revision=source['source_revision'],
                  artifact=dict(name=ARTIFACTS[platform], bytes=artifact.stat().st_size, sha256=digest(artifact)),
                  components={name: source['components'][name]['sha256'] for name in ('ori-core', 'ori-supervisor')},
                  manual_sha256=source['manual_sha256'], signing=evidence['signing'])
    record['components']['ui'] = evidence['ui_sha256']
    check_manifest(record, platform, record['source_revision'], artifact)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        folder = Path(tmp) / 'candidate'
        folder.mkdir()
        shutil.copyfile(artifact, folder / ARTIFACTS[platform])
        write_json(folder / f'{platform}-manifest.json', record)
        write_sums(folder)
        check_manifest(record, platform, record['source_revision'], folder / ARTIFACTS[platform])
        folder.rename(output)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('platform', choices=ARTIFACTS)
    p.add_argument('--metadata', type=Path, required=True)
    p.add_argument('--signing', type=Path)
    p.add_argument('--artifact', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    source = read_json(args.metadata, 32 * 1024 * 1024)
    if args.platform == 'pi':
        evidence = dict(source_revision=source['source_revision'], ui_sha256=source['web_assets']['/modern/index.html'],
                        signing={'kind': 'debian-sha256'})
    else:
        require(args.signing is not None, 'native verification receipt is required')
        evidence = read_json(args.signing)
    freeze(args.platform, source, evidence, args.artifact, args.out)


if __name__ == '__main__':
    main()
