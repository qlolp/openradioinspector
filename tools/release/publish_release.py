"""Publish verified prebuilt packages to immutable GitHub Releases, or resume an owned draft."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import release_acceptance as gate
from .macos_rc_package import digest, read_json, write_json
from .release_artifacts import (ARTIFACTS, LINUX_AMD64, LINUX_CI, LINUX_RELEASE_TAG, STABLE_PLATFORMS,
                                check_manifest, keys, platform_tag, require, sha, verify_sums, write_sums)


BUILDER = 'signed-candidates.yml'
BRANCHES = {'main', 'release/0.4.0'}
FINAL_RELEASES = {'stable': ('v0.4.0', STABLE_PLATFORMS), 'linux-amd64-stable': (LINUX_RELEASE_TAG, (LINUX_AMD64,))}
JOBS = {
    'windows-core.yml': {f'synthetic-{os}-latest-py{version}' for os in ('ubuntu', 'windows', 'macos')
                         for version in ('3.10', '3.12', '3.14', '3.15')},
    'modern-core.yml': {f'modern-{os}' for os in ('ubuntu-latest', 'windows-latest', 'macos-latest', 'ubuntu-24.04-arm')}
                       | {'Panorama storage on ubuntu-24.04-arm', 'Panorama storage on windows-latest'},
    'desktop-packages.yml': {'python', 'desktop (macos-latest)', 'desktop (windows-latest)',
                             'native-review-regressions', 'macos-personal-rc'},
    'modern-clients.yml': {'web', 'native-bridge', 'windows-installer'},
    'pi-rc.yml': {'native-arm64'},
    LINUX_CI: {'Build Debian amd64', 'Install on Debian 12 amd64', 'Install on Debian 13 amd64'},
}


def validate_run(run, jobs, repo, revision, workflow, platform=None):
    require(run.get('path') == '.github/workflows/' + workflow and run.get('head_sha') == revision
            and run.get('event') in ('push', 'workflow_dispatch') and run.get('head_branch') in BRANCHES
            and run.get('head_repository', {}).get('full_name') == repo and run.get('status') == 'completed'
            and run.get('conclusion') == 'success', 'workflow provenance or conclusion does not match')
    required = {f'build-{platform}'} if workflow == BUILDER else JOBS[workflow]
    require(required <= {job['name'] for job in jobs} and all(job.get('conclusion') == 'success' for job in jobs),
            f'missing, skipped or failed jobs in {workflow}')
    if workflow == BUILDER:
        require(run['event'] == 'workflow_dispatch', 'trusted signing dispatch required')


def verify_prerelease(release, ref, platform, revision, number=1):
    require(release.get('immutable') is True and release.get('prerelease') is True and release.get('draft') is False
            and release.get('tag_name') == platform_tag(platform, number) and ref.get('object', {}).get('type') == 'commit'
            and ref['object'].get('sha') == revision, 'required immutable platform prerelease missing or wrong source')


def github(*args, data=None, optional=False, token=None, output=None):
    env = os.environ.copy()
    if token is not None:
        env['GH_TOKEN'] = token
    proc = subprocess.run(['gh', 'api', *args, *(['--input', '-'] if data is not None else [])],
                          env=env, input=json.dumps(data).encode() if data is not None else None,
                          stdout=output or subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        if optional and b'(HTTP 404)' in proc.stderr:
            return None
        raise ValueError('GitHub API request failed: ' + proc.stderr.decode(errors='replace')[:500])
    return None if output else json.loads(proc.stdout)


class Repository:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.revision = os.environ['GITHUB_SHA']
        require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repo) and sha(self.revision, 40)
                and os.getenv('GITHUB_REF_PROTECTED') == 'true' and os.getenv('GITHUB_EVENT_NAME') == 'workflow_dispatch'
                and os.getenv('GITHUB_REF') in {'refs/heads/' + b for b in BRANCHES}, 'protected release dispatch required')
        require(subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == self.revision,
                'checkout and dispatched revision differ')
        subprocess.run(['git', 'merge-base', '--is-ancestor', self.revision, 'refs/remotes/origin/main'], check=True)
        self.base = f'repos/{self.repo}/'

    def api(self, path, **options):
        return github(self.base + path, **options)

    def run(self, run_id, workflow, platform=None, *, attempt=None):
        require(type(run_id) is int and run_id > 0, 'invalid workflow run ID')
        path = f'actions/runs/{run_id}'
        if attempt is not None:
            require(type(attempt) is int and attempt > 0, 'invalid workflow attempt')
            path += f'/attempts/{attempt}'
        run = self.api(path)
        actual_attempt = run['run_attempt']
        require(type(actual_attempt) is int and actual_attempt > 0 and attempt in (None, actual_attempt),
                'workflow attempt differs from the frozen manifest')
        results = self.api(f'actions/runs/{run_id}/attempts/{actual_attempt}/jobs?per_page=100')
        require(results['total_count'] == len(results['jobs']), 'incomplete CI job inventory')
        validate_run(run, results['jobs'], self.repo, self.revision, workflow, platform)
        return actual_attempt

    def immutable(self):
        require(self.api('immutable-releases', token=os.getenv('ORI_POLICY_READ_TOKEN') or None).get('enabled') is True,
                'enable immutable Releases before publishing')

    def tag_ref(self, tag, optional=False):
        ref = self.api('git/ref/tags/' + tag, optional=optional)
        if ref is None:
            return None
        obj = ref['object']
        for _ in range(8):
            if obj['type'] == 'commit':
                return {'object': obj}
            require(obj['type'] == 'tag' and sha(obj['sha'], 40), 'invalid release tag reference')
            obj = self.api('git/tags/' + obj['sha'])['object']
        raise ValueError('release tag chain is too deep')

    def published(self, platform, number, destination):
        tag = platform_tag(platform, number)
        release = self.api('releases/tags/' + tag)
        verify_prerelease(release, self.tag_ref(tag), platform, self.revision, number)
        assets = release['assets']
        names = platform_assets(platform) | {'SHA256SUMS'}
        require(len(assets) == len(names) and {a['name'] for a in assets} == names, 'prerelease assets differ')
        destination.mkdir()
        for asset in assets:
            path = destination / asset['name']
            with path.open('xb') as stream:
                github('-H', 'Accept: application/octet-stream', self.base + f"releases/assets/{asset['id']}", output=stream)
            compare_remote(asset, path)
        verify_sums(destination, names - {'SHA256SUMS'})
        row = accepted(self, destination, platform)
        index = read_json(destination / 'release.json')
        require(index == release_index(tag, self.revision, {platform: row}), 'prerelease index differs')
        return row

    def publish(self, folder, tag, prerelease):
        self.immutable()
        body = f'OpenRadioInspector 0.4.0\n\nSource commit `{self.revision}`\n\nSHA256SUMS `{digest(folder / "SHA256SUMS")}`'
        expected = dict(tag_name=tag, name=tag, body=body, target_commitish=self.revision, prerelease=prerelease)
        # The by-tag endpoint only promises published releases. Enumerate drafts
        # too, to safely resume after an interrupted asset upload.
        pages = github('--paginate', '--slurp', self.base + 'releases?per_page=100')
        matches = [r for page in pages for r in page if r['tag_name'] == tag]
        require(len(matches) <= 1, 'ambiguous release tag')
        if matches:
            release = matches[0]
            require(all(release.get(k) == v for k, v in expected.items()), 'different release already uses this tag')
        else:
            require(self.tag_ref(tag, optional=True) is None, 'existing tag has no matching release')
            release = github('-X', 'POST', self.base + 'releases', data={**expected, 'draft': True})
        paths = {p.name: p for p in folder.iterdir()}
        endpoint = f"releases/{release['id']}"
        existing = release['assets']
        require(len({a['name'] for a in existing}) == len(existing) and all(a['name'] in paths for a in existing),
                'unexpected assets on existing release')
        for asset in existing:
            compare_remote(asset, paths[asset['name']])
        if release['draft']:
            ref = self.tag_ref(tag, optional=True)
            require(ref is None or ref['object']['sha'] == self.revision, 'tag moved before publication')
            for name in sorted(paths.keys() - {a['name'] for a in existing}):
                subprocess.run(['gh', 'release', 'upload', tag, str(paths[name]), '--repo', self.repo], check=True)
            verify_remote_assets(self.api(endpoint), paths)
            self.immutable()
            release = github('-X', 'PATCH', self.base + endpoint,
                             data={'draft': False, 'make_latest': 'true' if tag == 'v0.4.0' and not prerelease else 'false'})
        require(release.get('immutable') is True and release['draft'] is False
                and self.tag_ref(tag)['object']['sha'] == self.revision, 'published release identity is unconfirmed')
        verify_remote_assets(self.api(endpoint), paths)
        print(release['html_url'])


def compare_remote(asset, path):
    require(asset['state'] == 'uploaded' and asset['size'] == path.stat().st_size
            and asset.get('digest') == 'sha256:' + digest(path), 'remote release asset differs: ' + path.name)


def verify_remote_assets(release, paths):
    assets = release['assets']
    require(len(assets) == len(paths) and {a['name'] for a in assets} == paths.keys(), 'remote asset set incomplete')
    for asset in assets:
        compare_remote(asset, paths[asset['name']])


def verify_github_attestation(repo, receipt, proof):
    # This is a Sigstore bundle issued by GitHub Actions. Signature, subject
    # digest, protected workflow identity and exact source are all enforced.
    base = ['gh', 'attestation', 'verify', str(receipt), '--bundle', str(proof),
            '--repo', repo.repo, '--signer-workflow', repo.repo + '/.github/workflows/review-release-evidence.yml',
            '--signer-digest', repo.revision, '--source-digest', repo.revision, '--deny-self-hosted-runners']
    for branch in sorted(BRANCHES):
        result = subprocess.run([*base, '--source-ref', 'refs/heads/' + branch],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
    raise ValueError('no valid GitHub attestation from the protected acceptance workflow at this source')


def fetch_witness(repo, receipt, proof):
    if os.getenv('ORI_SIGNATURE_B64'):
        collect_dispatch_file(proof.parent, proof.name, 'ORI_SIGNATURE_B64', 4096)
        return
    # Current GitHub responses can carry either inline data or an expiring
    # bundle_url. The official CLI handles both and writes one JSONL record per bundle.
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['gh', 'attestation', 'download', str(receipt.resolve()), '--repo', repo.repo,
                        '--predicate-type', 'https://slsa.dev/provenance/v1', '--limit', '100'],
                       cwd=tmp, check=True)
        files = list(Path(tmp).glob('*.jsonl'))
        require(len(files) == 1, 'GitHub returned no acceptance bundles')
        with files[0].open('rb') as records:
            for raw in iter(lambda: records.readline(128 * 1024 + 1), b''):
                require(len(raw) <= 128 * 1024, 'oversized GitHub acceptance bundle')
                write_json(proof, json.loads(raw))
                try:
                    verify_github_attestation(repo, receipt, proof)
                except ValueError:
                    continue
                return
    raise ValueError('submit an authorized SSH signature or complete the protected GitHub review of these exact bytes')


def platform_assets(platform):
    return {ARTIFACTS[platform], f'{platform}-manifest.json', f'{platform}-acceptance.json',
            f'{platform}-acceptance.proof', 'release.json'}


def release_index(tag, revision, packages):
    return {'schema_version': 1, 'tag': tag, 'source_revision': revision,
            'artifacts': {platform: row['artifact'] for platform, row in sorted(packages.items())}}


def verify_software_runs(repo, acceptance, manifest):
    for workflow, run_id in acceptance['software_runs'].items():
        if workflow == LINUX_CI:
            repo.run(run_id, workflow, attempt=manifest['native_ci']['run_attempt'])
        else:
            repo.run(run_id, workflow)


def accepted(repo, folder, platform, expected_run=None):
    build = folder / f'{platform}-manifest.json'
    manifest = read_json(build)
    check_manifest(manifest, platform, repo.revision, folder / ARTIFACTS[platform])
    receipt = folder / f'{platform}-acceptance.json'
    proof = folder / f'{platform}-acceptance.proof'
    require(proof.is_file() and not proof.is_symlink() and proof.stat().st_size <= 128 * 1024, 'invalid witness proof')
    if proof.read_bytes().startswith(b'-----BEGIN SSH SIGNATURE-----'):
        gate.verify_signature(receipt, proof, platform, os.getenv('ORI_WITNESS_' + platform.upper().replace('-', '_')))
    else:
        verify_github_attestation(repo, receipt, proof)
    acceptance = read_json(receipt, 48 * 1024)
    run_id = expected_run if expected_run is not None else acceptance['build_run_id']
    gate.validate_receipt(acceptance, platform, repo.revision, manifest['artifact']['sha256'], digest(build), run_id,
                          native_ci=manifest.get('native_ci'))
    repo.run(run_id, BUILDER, platform)
    verify_software_runs(repo, acceptance, manifest)
    return manifest


def collect_dispatch_file(folder, name, variable, limit):
    raw = base64.b64decode(os.environ[variable], validate=True)
    require(0 < len(raw) <= limit, 'empty or oversized dispatch evidence')
    (folder / name).write_bytes(raw)


def promote(repo, platform, numbers, run_id, candidate, root):
    out = root / 'publish'
    out.mkdir()
    rows = {}
    if platform in FINAL_RELEASES:
        tag, platforms = FINAL_RELEASES[platform]
        for name in platforms:
            source = root / name
            rows[name] = repo.published(name, numbers[name], source)
            for asset in sorted(platform_assets(name) - {'release.json'}):
                shutil.copyfile(source / asset, out / asset)
    else:
        if platform in ('windows', 'pi'):
            repo.published('macos', numbers['macos'], root / 'accepted-mac')
        names = {ARTIFACTS[platform], f'{platform}-manifest.json'}
        verify_sums(candidate, names)
        for name in names:
            shutil.copyfile(candidate / name, out / name)
        collect_dispatch_file(out, f'{platform}-acceptance.json', 'ORI_RECEIPT_B64', 48 * 1024)
        fetch_witness(repo, out / f'{platform}-acceptance.json', out / f'{platform}-acceptance.proof')
        rows[platform] = accepted(repo, out, platform, run_id)
        tag = platform_tag(platform, numbers[platform])
    write_json(out / 'release.json', release_index(tag, repo.revision, rows))
    write_sums(out)
    repo.publish(out, tag, platform not in FINAL_RELEASES)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['origin', 'native-origin', 'publish'])
    parser.add_argument('platform', choices=[*ARTIFACTS, *FINAL_RELEASES])
    parser.add_argument('--build-run', type=int)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--numbers', default='{"macos":1,"windows":1,"pi":1}')
    parser.add_argument('--linux-rc', default='1')
    args = parser.parse_args()
    repo = Repository()
    if args.operation == 'native-origin':
        require(args.platform == LINUX_AMD64, 'only Debian amd64 freezes a native CI artifact')
    if args.platform in ARTIFACTS:
        if args.operation == 'native-origin':
            attempt = repo.run(args.build_run, LINUX_CI)
        else:
            attempt = repo.run(args.build_run, BUILDER, args.platform)
        if args.operation != 'publish' and os.getenv('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
                output.write(f'attempt={int(attempt)}\n')
    if args.operation != 'publish':
        return
    if args.platform in (LINUX_AMD64, 'linux-amd64-stable'):
        numbers = {LINUX_AMD64: int(args.linux_rc)}
    else:
        numbers = json.loads(args.numbers)
        keys(numbers, STABLE_PLATFORMS, 'platform RC numbers')
    for name, number in numbers.items():
        platform_tag(name, number)
    require(args.platform in FINAL_RELEASES or args.candidate is not None, 'prebuilt candidate required')
    with tempfile.TemporaryDirectory() as tmp:
        promote(repo, args.platform, numbers, args.build_run, args.candidate, Path(tmp))


if __name__ == '__main__':
    main()
