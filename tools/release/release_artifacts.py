"""Freeze native package bytes and emit a small public build manifest."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

from .macos_rc_package import digest, read_json, write_json


LINUX_AMD64 = 'linux-amd64'
LINUX_CI = 'linux-amd64.yml'
DEBIAN_VERSIONS = ('12', '13')
STABLE_PLATFORMS = ('macos', 'windows', 'pi')
LINUX_RELEASE_TAG = 'linux-amd64-v0.4.0'
ARTIFACTS = {
    'macos': 'OpenRadioInspector-0.4.0-macos-arm64.dmg',
    'windows': 'OpenRadioInspector-0.4.0-windows-x64.exe',
    'pi': 'OpenRadioInspector-0.4.0-pi-arm64.deb',
    LINUX_AMD64: 'OpenRadioInspector-0.4.0-linux-amd64.deb',
}
COMPONENTS = {'ori-core', 'ori-supervisor', 'ui'}
TARGETS = {'macos': 'macos-arm64', 'windows': 'windows-x64', 'pi': 'raspberrypi-arm64', LINUX_AMD64: LINUX_AMD64}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def platform_tag(platform, number):
    require(platform in ARTIFACTS and type(number) is int and 1 <= number <= 9999, 'invalid platform candidate tag')
    if platform == LINUX_AMD64:
        return f'{LINUX_RELEASE_TAG}-rc.{number}'
    return f'v0.4.0-{platform}.rc.{number}'


TAG = {name: platform_tag(name, 1) for name in ARTIFACTS}


def sha(value, size=64):
    return isinstance(value, str) and bool(re.fullmatch(rf'[0-9a-f]{{{size}}}', value))


def keys(value, expected, label):
    require(isinstance(value, dict) and value.keys() == set(expected), f'incomplete or unexpected {label} fields')


def checksum_list(hashes):
    return ''.join(f'{hashes[name]}  {name}\n' for name in sorted(hashes))


def sums(directory, names):
    hashes = {}
    for name in names:
        path = directory / name
        require(path.is_file() and not path.is_symlink(), 'unexpected release asset type')
        hashes[name] = digest(path)
    return checksum_list(hashes)


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
        require(proof == {'kind': 'debian-sha256'}, 'wrong Debian integrity mechanism')


def check_linux_ci(proof):
    keys(proof, ('run_id', 'run_attempt', 'installations'), 'native Debian CI provenance')
    require(all(type(proof[k]) is int and proof[k] > 0 for k in ('run_id', 'run_attempt')),
            'invalid native Debian CI run or attempt')
    keys(proof['installations'], DEBIAN_VERSIONS, 'native Debian lifecycle reports')
    for reports in proof['installations'].values():
        keys(reports, ('environment_sha256', 'lifecycle_sha256'), 'native Debian report digests')
        require(all(sha(v) for v in reports.values()), 'invalid native Debian report digest')
    require(len({r['environment_sha256'] for r in proof['installations'].values()}) == len(DEBIAN_VERSIONS),
            'distinct Debian 12 and 13 environments required')


def linux_candidate_ci(source, metadata, artifact, reports, revision, run_id, attempt):
    package, version = 'openradioinspector-linux', '0.4.0-1'
    require(sha(revision, 40) and source['source_revision'] == revision and source['release_version'] == '0.4.0'
            and source['target'] == 'x86_64-unknown-linux-gnu' and source['debian_architecture'] == 'amd64'
            and source['package'] == package and source['debian_version'] == version,
            'wrong native Debian package, target, revision or version')
    require(metadata == artifact.parent / 'release.json' and artifact.name == f'{package}_{version}_amd64.deb',
            'unexpected native Debian artifact layout')
    verify_sums(artifact.parent, {artifact.name, metadata.name, 'README.md'})
    fields = subprocess.check_output(['dpkg-deb', '--showformat=${Package} ${Version} ${Architecture}', '--show', artifact],
                                     text=True)
    require(fields == f'{package} {version} amd64', 'Debian control fields differ from the candidate')
    final, prior = version, '0.4.0~rc.1-1'
    phases = [('clean-install', final), ('install', prior), ('upgrade', final),
              ('rollback', prior), ('restart', prior), ('existing-v6', final)]
    package_hash = digest(artifact)
    proof = dict(run_id=run_id, run_attempt=attempt, installations={})
    for os_version in DEBIAN_VERSIONS:
        folder = reports / os_version
        environment, lifecycle = folder / 'environment.json', folder / 'lifecycle.jsonl'
        expected = dict(os_id='debian', os_version=os_version, architecture='amd64', package=package,
                        source_revision=revision, debian_version=version, deb_sha256=package_hash, hardware_validation='not_run')
        require(read_json(environment) == expected, 'native CI installed a different Debian package or OS')
        require(lifecycle.is_file() and not lifecycle.is_symlink() and lifecycle.stat().st_size <= 64 * 1024,
                'invalid native Debian lifecycle report')
        rows = [json.loads(line) for line in lifecycle.read_text(encoding='utf-8').splitlines()]
        require(len(rows) == len(phases), 'incomplete native Debian install, upgrade or rollback')
        for row, (phase, deb_version) in zip(rows, phases):
            require(row.get('phase') == phase and row.get('debian_version') == deb_version
                    and row.get('source_revision') == revision and row.get('result') == 'passed'
                    and type(row.get('web_assets_checked')) is int and row['web_assets_checked'] == len(source['web_assets']) > 0,
                    'native Debian lifecycle phase, revision or installed WebUI differs')
        proof['installations'][os_version] = dict(environment_sha256=digest(environment), lifecycle_sha256=digest(lifecycle))
    check_linux_ci(proof)
    return proof


def check_manifest(row, platform, revision, artifact):
    fields = ['schema_version', 'release_version', 'platform', 'target', 'source_revision', 'artifact', 'components',
              'manual_sha256', 'signing']
    keys(row, fields + (['native_ci'] if platform == LINUX_AMD64 else []), 'candidate manifest')
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
    if platform == LINUX_AMD64:
        check_linux_ci(row['native_ci'])


def freeze(platform, source, evidence, artifact, output):
    require(not output.exists() and not output.is_symlink(), 'candidate output already exists')
    require(source['source_revision'] == evidence['source_revision'], 'mixed package evidence')
    record = dict(schema_version=1, release_version=source['release_version'], platform=platform, target=TARGETS[platform], source_revision=source['source_revision'],
                  artifact=dict(name=ARTIFACTS[platform], bytes=artifact.stat().st_size, sha256=digest(artifact)),
                  components={name: source['components'][name]['sha256'] for name in ('ori-core', 'ori-supervisor')},
                  manual_sha256=source['manual_sha256'], signing=evidence['signing'])
    record['components']['ui'] = evidence['ui_sha256']
    if platform == LINUX_AMD64:
        record['native_ci'] = evidence['native_ci']
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
    p.add_argument('--native-reports', type=Path, help='Downloaded Debian 12 and 13 lifecycle artifacts in folders 12/ and 13/')
    p.add_argument('--source-revision')
    p.add_argument('--native-run', type=int)
    p.add_argument('--native-attempt', type=int)
    args = p.parse_args()
    source = read_json(args.metadata, 32 * 1024 * 1024)
    if args.platform in ('pi', LINUX_AMD64):
        evidence = dict(source_revision=source['source_revision'], ui_sha256=source['web_assets']['/modern/index.html'],
                        signing={'kind': 'debian-sha256'})
        if args.platform == LINUX_AMD64:
            require(args.native_reports is not None, 'native Debian CI artifacts are required')
            evidence['native_ci'] = linux_candidate_ci(source, args.metadata, args.artifact, args.native_reports,
                                                       args.source_revision, args.native_run, args.native_attempt)
        else:
            require(source['target'] == 'aarch64-unknown-linux-gnu', 'expected a Raspberry Pi arm64 build')
    else:
        require(args.signing is not None, 'native verification receipt is required')
        evidence = read_json(args.signing)
    freeze(args.platform, source, evidence, args.artifact, args.out)


if __name__ == '__main__':
    main()
