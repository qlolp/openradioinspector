"""Linux publication fixtures use local files and mocked GitHub responses."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_signed_release import HASH, NATIVE_CI, REV, receipt
from tools.release import bench_receipt, publish_release as publish, release_acceptance as gate, release_artifacts as bundles


class LinuxReceiptTests(unittest.TestCase):
    def check(self, row):
        gate.validate_receipt(row, 'linux-amd64', REV, HASH, HASH, 42, native_ci=NATIVE_CI)

    def test_linux_template_identifies_ci_and_leaves_both_bench_installs_and_hardware_unobserved(self):
        form = bench_receipt.template('linux-amd64', REV, HASH, HASH, 42, native_ci=NATIVE_CI)
        self.assertEqual(form['software_runs'], {'linux-amd64.yml': 100})
        self.assertEqual(set(form['installation']), {'12', '13'})
        self.assertIsNone(form['installation']['12']['clean_install'])
        self.assertIsNone(form['hardware']['physical'])
        with self.assertRaises(ValueError):
            self.check(form)

    def test_linux_acceptance_requires_exact_package_on_both_debians_and_physical_debian_13(self):
        original = receipt('linux-amd64')
        self.check(original)
        changes = (
            (('software_runs', 'linux-amd64.yml'), 101),
            (('installation',), {'13': original['installation']['13']}),
            (('installation', '12', 'clean_install'), False),
            (('installation', '13', 'offline'), False),
            (('installation', '12', 'artifact_sha256'), 'f' * 64),
            (('installation', '13', 'prior_artifact_sha256'), HASH),
            (('installation', '13', 'report_sha256'), original['installation']['12']['report_sha256']),
            (('hardware', 'os_id'), 'ubuntu'), (('hardware', 'os_version'), '12'),
            (('hardware', 'architecture'), 'arm64'), (('hardware', 'physical'), False),
            (('hardware', 'artifact_sha256'), 'f' * 64), (('hardware', 'seconds'), 28799),
            (('hardware', 'devices'), ['tinySA Ultra']), (('hardware', 'duplicates'), 1),
            (('loads', 'three-clients', 'concurrent_clients'), 2),
            (('loads', 'three-clients', 'archived', 'pcm_samples'), 86399999),
            (('loads', 'three-clients', 'pcm_rate_hz'), 44100),
            (('loads', 'three-clients', 'online_export_verified'), False),
            (('scan', '30000', 'passes'), 9), (('storage', '4096', 'verified_sweeps'), 511),
        )
        for path, value in changes:
            with self.subTest(path=path):
                row = copy.deepcopy(original)
                obj = row
                for key in path[:-1]:
                    obj = obj[key]
                obj[path[-1]] = value
                with self.assertRaises(ValueError):
                    self.check(row)

    def test_private_install_and_hardware_records_must_match_the_signed_host_and_package(self):
        row = receipt('linux-amd64')
        hw = {k: v for k, v in row['hardware'].items() if k != 'report_sha256'}
        hw['source_revision'] = REV
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for version, install in row['installation'].items():
                path = folder / f'installation-debian-{version}.json'
                original = {k: v for k, v in install.items() if k != 'report_sha256'}
                original.update(os_id='debian', os_version=version, architecture='amd64', source_revision=REV)
                bundles.write_json(path, original)
                install['report_sha256'] = bundles.digest(path)
            bench_receipt.compare_linux_bench(row, folder, hw)
            with self.assertRaises(ValueError):
                bench_receipt.compare_linux_bench(row, folder, dict(hw, physical=1))
            path = folder / 'installation-debian-12.json'
            bundles.write_json(path, dict(bundles.read_json(path), os_version='13'))
            row['installation']['12']['report_sha256'] = bundles.digest(path)
            with self.assertRaises(ValueError):
                bench_receipt.compare_linux_bench(row, folder, hw)


class NativeDebianEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / 'package'
        package.mkdir()
        self.artifact = package / 'openradioinspector-linux_0.4.0-1_amd64.deb'
        self.artifact.write_bytes(b'native-ci fixture')
        self.source = dict(package='openradioinspector-linux', release_version='0.4.0', debian_version='0.4.0-1',
                           target='x86_64-unknown-linux-gnu', debian_architecture='amd64', source_revision=REV,
                           web_assets={'/modern/index.html': HASH})
        self.metadata = package / 'release.json'
        bundles.write_json(self.metadata, self.source)
        (package / 'README.md').write_text('offline guide')
        bundles.write_sums(package)
        self.reports = root / 'reports'
        for version in ('12', '13'):
            folder = self.reports / version
            folder.mkdir(parents=True)
            env = dict(os_id='debian', os_version=version, architecture='amd64', package='openradioinspector-linux',
                       source_revision=REV, debian_version='0.4.0-1', deb_sha256=bundles.digest(self.artifact),
                       hardware_validation='not_run')
            bundles.write_json(folder / 'environment.json', env)
            phases = [('clean-install', False), ('install', True), ('upgrade', False),
                      ('rollback', True), ('restart', True), ('existing-v6', False)]
            with (folder / 'lifecycle.jsonl').open('w', encoding='utf-8') as out:
                for phase, rc in phases:
                    print(json.dumps(dict(phase=phase, source_revision=REV, debian_version='0.4.0~rc.1-1' if rc else '0.4.0-1',
                                          web_assets_checked=1, result='passed')), file=out)
        dpkg = patch.object(bundles.subprocess, 'check_output', return_value='openradioinspector-linux 0.4.0-1 amd64')
        self.control = dpkg.start()
        self.addCleanup(dpkg.stop)

    def verify(self):
        return bundles.linux_candidate_ci(self.source, self.metadata, self.artifact, self.reports, REV, 100, 2)

    def test_freeze_binds_both_installations_and_the_actual_package_control_fields(self):
        result = self.verify()
        self.assertEqual((result['run_id'], result['run_attempt']), (100, 2))
        for version in ('12', '13'):
            self.assertEqual(result['installations'][version]['environment_sha256'],
                             bundles.digest(self.reports / version / 'environment.json'))
        self.control.return_value = 'openradioinspector-pi-rc 0.4.0-1 arm64'
        with self.assertRaisesRegex(ValueError, 'control fields'):
            self.verify()
        self.control.return_value = 'openradioinspector-linux 0.4.0-1 amd64'
        self.artifact.write_bytes(b'other candidate')
        bundles.write_sums(self.artifact.parent)
        with self.assertRaisesRegex(ValueError, 'different Debian package'):
            self.verify()

    def test_freeze_rejects_swapped_hosts_and_incomplete_or_wrong_revision_upgrades(self):
        path = self.reports / '13' / 'environment.json'
        original = path.read_bytes()
        path.write_bytes((self.reports / '12' / 'environment.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'different Debian package or OS'):
            self.verify()
        path.write_bytes(original)
        phases = self.reports / '12' / 'lifecycle.jsonl'
        rows = [json.loads(line) for line in phases.read_text().splitlines()]
        for corrupted in (rows[1:], [*rows[:2], dict(rows[2], source_revision='a' * 40), *rows[3:]],
                          [*rows[:2], dict(rows[2], debian_version='0.4.0~rc.1-1'), *rows[3:]]):
            phases.write_text('\n'.join(map(json.dumps, corrupted)) + '\n')
            with self.assertRaises(ValueError):
                self.verify()


class LinuxPublicationPolicyTests(unittest.TestCase):
    def test_each_final_release_uses_only_its_own_rc_numbers(self):
        legacy = '{"macos":2,"windows":3,"pi":4}'
        for platform, numbers, linux_rc, expected in (
                ('stable', legacy, '', {'macos': 2, 'windows': 3, 'pi': 4}),
                ('linux-amd64-stable', '', '7', {'linux-amd64': 7})):
            with self.subTest(platform=platform), patch.object(publish, 'Repository'), patch.object(publish, 'promote') as promote, \
                    patch('sys.argv', ['publish_release', 'publish', platform, '--numbers', numbers, '--linux-rc', linux_rc]):
                publish.main()
                self.assertEqual(promote.call_args.args[1:3], (platform, expected))
        with patch.object(publish, 'Repository'), patch.object(publish, 'promote') as promote, \
                patch('sys.argv', ['publish_release', 'publish', 'stable', '--numbers', '{"linux-amd64":1}']):
            with self.assertRaises(ValueError):
                publish.main()
            promote.assert_not_called()

    def test_native_ci_verifies_both_jobs_at_the_recorded_attempt(self):
        repo = publish.Repository.__new__(publish.Repository)
        repo.repo, repo.revision = 'owner/project', REV
        run = dict(path='.github/workflows/linux-amd64.yml', head_sha=REV, head_branch='main', event='push',
                   head_repository={'full_name': repo.repo}, status='completed', conclusion='success', run_attempt=2)
        jobs = [dict(name=n, conclusion='success') for n in ('Build Debian amd64', 'Install on Debian 12 amd64', 'Install on Debian 13 amd64')]
        run_url = 'actions/runs/100/attempts/2'
        for required in (jobs, jobs[:2], [jobs[0], jobs[2]]):
            repo.api = Mock(side_effect={run_url: run, run_url + '/jobs?per_page=100':
                                         {'total_count': len(required), 'jobs': required}}.__getitem__)
            if required == jobs:
                self.assertEqual(repo.run(100, 'linux-amd64.yml', attempt=2), 2)
            else:
                with self.assertRaisesRegex(ValueError, 'missing, skipped or failed'):
                    repo.run(100, 'linux-amd64.yml', attempt=2)
        repo.api = Mock(return_value=dict(run, run_attempt=3))
        with self.assertRaisesRegex(ValueError, 'attempt differs'):
            repo.run(100, 'linux-amd64.yml', attempt=2)

    def test_only_combined_stable_is_marked_latest_on_the_publication_request(self):
        repo = publish.Repository.__new__(publish.Repository)
        repo.repo, repo.revision, repo.base = 'owner/project', REV, 'repos/owner/project/'
        repo.immutable = Mock()
        repo.tag_ref = Mock(return_value={'object': {'type': 'commit', 'sha': REV}})
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            sums = folder / 'SHA256SUMS'
            sums.write_bytes(b'fixture')
            asset = dict(name='SHA256SUMS', size=sums.stat().st_size, state='uploaded', digest='sha256:' + bundles.digest(sums))
            repo.api = Mock(return_value={'assets': [asset]})
            for tag, prerelease, latest in [('linux-amd64-v0.4.0-rc.2', True, 'false'), ('linux-amd64-v0.4.0', False, 'false'),
                                            ('v0.4.0', False, 'true')]:
                with self.subTest(tag=tag):
                    body = f'OpenRadioInspector 0.4.0\n\nSource commit `{REV}`\n\nSHA256SUMS `{bundles.digest(sums)}`'
                    draft = dict(tag_name=tag, name=tag, body=body, target_commitish=REV, prerelease=prerelease, draft=True,
                                 assets=[asset], id=7, html_url='https://example.invalid/release')
                    with patch.object(publish, 'github', side_effect=[[[draft]], dict(draft, draft=False, immutable=True)]) as api, \
                            patch.object(publish.subprocess, 'run', side_effect=AssertionError('unexpected upload')), patch('builtins.print'):
                        repo.publish(folder, tag, prerelease)
                    api.assert_called_with('-X', 'PATCH', repo.base + 'releases/7', data={'draft': False, 'make_latest': latest})


if __name__ == '__main__':
    unittest.main()
