"""Public promotion tests. All signed bytes, keys, artifacts and runs here are fixtures."""
import base64
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tools.release import release_artifacts as bundles, release_acceptance as gate, publish_release as publish


REV = 'b' * 40
HASH = 'e' * 64


def receipt(platform):
    counts = {'sweeps': 18000, 'observations': 18000, 'pcm_samples': 0 if platform == 'macos' else 86400000}
    work = {'seconds': 1800, 'points_per_sweep': 4096, 'history_points_per_sweep': 290,
            'pcm_rate_hz': None if platform == 'macos' else 48000, 'pcm_channels': None if platform == 'macos' else 1,
            'online_history_verified': True, 'online_export_verified': True, 'online_backup_verified': True, 'offered': counts.copy(), 'archived': counts.copy(), 'offered_sweep_ids_sha256': HASH,
            'reaction_p95_ms': 100, 'queue_p95_ms': 200, 'commit_to_display_p95_ms': 250, 'pan_min_fps': 30,
            'history_first_100_ms': 500, 'history_sweeps': 1000000, 'concurrent_clients': 3,
            'backup_restore_verified': True, 'report_sha256': '1' * 64}
    transports = ['binary-v1', 'sse', 'three-clients'] if platform == 'macos' else ['three-clients']
    loads = {}
    for i, name in enumerate(transports):
        clients = 3 if name == 'three-clients' else 1
        loads[name] = dict(work, concurrent_clients=clients, report_sha256=str(i + 6) * 64,
                           displays=[{'client': n + 1, 'presented_sweeps': 18000, 'presented_sweep_ids_sha256': HASH}
                                     for n in range(clients)])
    scan = {'start_hz': 790000000, 'stop_hz': 805000000, 'step_hz': 10000, 'passes': 10,
            'direct_profile_sha256': HASH, 'installed_profile_sha256': HASH, 'direct_elapsed_seconds': 90,
            'installed_elapsed_seconds': 100, 'direct_reference_sha256': '1' * 64, 'export_sha256': '2' * 64}
    storage = {'sweeps': 512, 'native': True, 'verified_sweeps': 512, 'verified_events': 512,
               'v6_source_sha256': HASH, 'v7_source_sha256': HASH, 'v6_growth_bytes': 10000, 'v7_growth_bytes': 7000,
               'v6_main_bytes': 20000, 'v7_main_bytes': 14000}
    return {'schema_version': 1, 'platform': platform, 'target': gate.TARGETS[platform], 'source_revision': REV,
            'build_run_id': 42, 'artifact_sha256': HASH, 'manifest_sha256': HASH,
            'software_runs': {workflow: 100 + i for i, workflow in enumerate(gate.required_workflows(platform))},
            'installation': {'offline': True, 'clean_install': True, 'upgrade': True, 'rollback': True,
                             'new_measurements_preserved': True, 'manual_opened_offline': True,
                             'prior_artifact_sha256': 'a' * 64, 'report_sha256': '3' * 64},
            'loads': loads, 'scan': {str(n): dict(scan, report_sha256=c * 64) for n, c in [(10000, 'b'), (30000, 'c')]},
            'storage': {str(n): dict(storage, report_sha256=c * 64) for n, c in [(290, 'd'), (4096, 'e')]},
            'protocol_report_sha256': '4' * 64, 'protocol_passed': True,
            'hardware': {'physical': True, 'devices': ['tinySA Ultra', 'AR-DV1'], 'seconds': 28800,
                         'sweeps': 4852, 'observations': 4312, 'unexplained_losses': 0,
                         'duplicates': 0, 'report_sha256': '5' * 64}}


class ReceiptTests(unittest.TestCase):
    def check(self, row, platform):
        gate.validate_receipt(row, platform, REV, HASH, HASH, 42)

    def test_real_benchmarks_and_installation_are_independent_of_successful_builds(self):
        for platform in ('macos', 'windows', 'pi'):
            valid = receipt(platform)
            self.check(valid, platform)
            transport = next(iter(valid['loads']))
            for path, value in (
                (('source_revision',), 'a' * 40), (('build_run_id',), 41), (('manifest_sha256',), 'f' * 64),
                (('target',), 'linux-x64'), (('installation', 'offline'), False),
                (('installation', 'rollback'), 'pending'), (('installation', 'prior_artifact_sha256'), HASH),
                (('protocol_passed',), False), (('hardware', 'seconds'), 28799),
                (('hardware', 'physical'), 1), (('hardware', 'sweeps'), True),
                (('hardware', 'devices'), ['simulator', 'AR-DV1']), (('hardware', 'unexplained_losses'), 1),
                (('loads', transport, 'seconds'), 1799), (('loads', transport, 'points_per_sweep'), 290),
                (('loads', transport, 'history_points_per_sweep'), 1), (('loads', transport, 'pcm_rate_hz'), 8000),
                (('loads', transport, 'pcm_channels'), 2), (('loads', transport, 'online_export_verified'), False), (('loads', transport, 'reaction_p95_ms'), 100.01),
                (('loads', transport, 'archived', 'sweeps'), 17999), (('loads', transport, 'displays', 0, 'presented_sweeps'), 17999),
                (('loads', transport, 'queue_p95_ms'), float('nan')), (('software_runs',), {}),
                (('loads', 'three-clients', 'displays'), valid['loads']['three-clients']['displays'][:2]),
                (('loads', 'three-clients', 'displays', 2, 'presented_sweep_ids_sha256'), '3' * 64),
                (('scan', '10000', 'direct_elapsed_seconds'), 89.99),
                (('scan', '30000', 'installed_profile_sha256'), '9' * 64),
                (('storage', '290', 'v7_growth_bytes'), 7001), (('storage', '4096', 'v7_main_bytes'), 14001),
                (('storage', '290', 'v7_source_sha256'), '9' * 64),
                (('operator_private_serial_number',), 'not for upload')):
                with self.subTest(platform=platform, path=path):
                    invalid = copy.deepcopy(valid)
                    node = invalid
                    for key in path[:-1]:
                        node = node[key]
                    node[path[-1]] = value
                    with self.assertRaises(ValueError):
                        self.check(invalid, platform)
        mac = receipt('macos')
        mac['loads']['sse']['report_sha256'] = mac['loads']['binary-v1']['report_sha256']
        with self.assertRaises(ValueError):
            self.check(mac, 'macos')

    def test_display_projection_detects_losing_one_of_54000_actual_renders(self):
        from tools.release.bench_receipt import display_ledger
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'offered.sqlite'
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript("PRAGMA application_id=1330792770; PRAGMA user_version=1;"
                                 "CREATE TABLE offers(id TEXT PRIMARY KEY, kind TEXT, status TEXT, verified INTEGER);"
                                 "CREATE TABLE rendered(id TEXT, client TEXT, PRIMARY KEY (id,client));")
                db.executemany("INSERT INTO offers VALUES (?,'sweep','accepted',1)", ((str(i),) for i in range(18000)))
                for client in ('0', '1', '2'):
                    db.execute("INSERT INTO rendered SELECT id, ? FROM offers", (client,))
            full = display_ledger(path)
            self.assertEqual([18000] * 3, [client['presented_sweeps'] for client in full['displays']])
            self.assertTrue(all(client['presented_sweep_ids_sha256'] == full['offered_sweep_ids_sha256']
                                for client in full['displays']))
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("DELETE FROM rendered WHERE client='2' AND id='1234'")
            with self.assertRaises(ValueError):
                display_ledger(path)

    def test_signature_requires_the_preconfigured_platform_witness_and_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / 'receipt.json'
            evidence.write_text(json.dumps(receipt('pi')) + '\n')
            for key in ('authorized', 'untrusted'):
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / key)], check=True)
            allowed = (root / 'authorized.pub').read_text().strip()
            for key in ('untrusted', 'authorized'):
                signature = root / 'receipt.json.sig'
                if signature.exists():
                    signature.unlink()
                subprocess.run(['ssh-keygen', '-q', '-Y', 'sign', '-f', str(root / key), '-n',
                                gate.NAMESPACE + '/pi', str(evidence)], check=True)
                if key == 'untrusted':
                    with self.assertRaises(ValueError):
                        gate.verify_signature(evidence, signature, 'pi', allowed)
                else:
                    gate.verify_signature(evidence, signature, 'pi', allowed)
            with self.assertRaises(ValueError):
                gate.verify_signature(evidence, signature, 'windows', allowed)
            evidence.write_bytes(evidence.read_bytes() + b' ')
            with self.assertRaises(ValueError):
                gate.verify_signature(evidence, signature, 'pi', allowed)


class PublicationTests(unittest.TestCase):
    def test_software_provenance_rejects_skips_forks_wrong_commit_and_wrong_workflow(self):
        repo = 'owner/project'
        good = {'path': '.github/workflows/modern-clients.yml', 'status': 'completed', 'conclusion': 'success',
                'head_sha': REV, 'event': 'push', 'head_branch': 'main', 'head_repository': {'full_name': repo}}
        jobs = [{'name': name, 'conclusion': 'success'} for name in ['web', 'native-bridge', 'windows-installer']]
        publish.validate_run(good, jobs, repo, REV, 'modern-clients.yml')
        for field, value in [('head_sha', 'a' * 40), ('path', '.github/workflows/else.yml'), ('event', 'pull_request'),
                             ('head_repository', {'full_name': 'fork/repo'}), ('conclusion', 'skipped')]:
            with self.assertRaises(ValueError):
                publish.validate_run(dict(good, **{field: value}), jobs, repo, REV, 'modern-clients.yml')
        for bad_jobs in [[], jobs[:-1], [*jobs[:-1], dict(jobs[-1], conclusion='skipped')]]:
            with self.assertRaises(ValueError):
                publish.validate_run(good, bad_jobs, repo, REV, 'modern-clients.yml')

    def test_github_review_uses_cli_bundle_download_and_archives_only_verified_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt_path = root / 'macos-acceptance.json'
            receipt_path.write_text('{}')
            proof = root / 'macos-acceptance.proof'
            repo = SimpleNamespace(repo='owner/project', revision=REV)
            def download(args, **kwargs):
                self.assertEqual(args[:3], ['gh', 'attestation', 'download'])
                (Path(kwargs['cwd']) / 'sha256-example.jsonl').write_text('{"id":1}\n{"id":2}\n')
                return SimpleNamespace(returncode=0)
            def verify(repository, receipt_file, bundle):
                self.assertIs(repository, repo)
                self.assertEqual(receipt_file, receipt_path)
                if json.loads(bundle.read_bytes()) != {'id': 2}:
                    raise ValueError('unrelated workflow attestation')
            with patch.dict(os.environ, {'ORI_SIGNATURE_B64': ''}), patch.object(publish.subprocess, 'run', side_effect=download), \
                    patch.object(publish, 'verify_github_attestation', side_effect=verify):
                publish.fetch_witness(repo, receipt_path, proof)
            self.assertEqual(json.loads(proof.read_bytes()), {'id': 2})

    def test_promotes_only_immutable_platform_prereleases_at_matching_git_commit(self):
        release = {'immutable': True, 'prerelease': True, 'draft': False, 'tag_name': 'v0.4.0-macos.rc.1'}
        ref = {'object': {'type': 'commit', 'sha': REV}}
        publish.verify_prerelease(release, ref, 'macos', REV)
        for key, value in [('immutable', False), ('draft', True), ('prerelease', False), ('tag_name', 'v0.4.0')]:
            with self.assertRaises(ValueError):
                publish.verify_prerelease(dict(release, **{key: value}), ref, 'macos', REV)
        with self.assertRaises(ValueError):
            publish.verify_prerelease(release, {'object': {'type': 'commit', 'sha': 'a' * 40}}, 'macos', REV)

    def test_checksums_cover_actual_files_and_reject_replacement_or_extra_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'first.deb').write_bytes(b'fixture')
            (root / 'second.json').write_bytes(b'sanitized')
            bundles.write_sums(root)
            bundles.verify_sums(root, {'first.deb', 'second.json'})
            (root / 'second.json').write_bytes(b'edited')
            with self.assertRaises(ValueError):
                bundles.verify_sums(root, {'first.deb', 'second.json'})
            (root / 'second.json').write_bytes(b'sanitized')
            (root / 'private.log').touch()
            with self.assertRaises(ValueError):
                bundles.verify_sums(root, {'first.deb', 'second.json'})

    def test_each_prerelease_requires_accepted_mac_and_stable_copies_all_three_exact_packages(self):
        # No network or external credentials. The promotion code receives the same files
        # as a native workflow would deliver, with short-lived test-only witness keys.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / 'witness'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            public_key = key.with_suffix('.pub').read_text().strip()
            public = {}
            candidates = {}
            signed = {}
            for platform, filename in bundles.ARTIFACTS.items():
                candidate = root / ('candidate-' + platform)
                raw = root / filename
                raw.write_bytes((platform + ' frozen test bytes').encode())
                proof = {'macos': {'kind': 'developer-id', 'certificate_sha1': 'c' * 40, 'team_id': 'ABCD123456',
                                   'app_ticket': 'be551760-c774-42c5-bb86-fd738ed6b1cb', 'dmg_ticket': '31d00aaf-76dc-4c6f-892a-da934655da32',
                                   'stapled': True, 'timestamp': True, 'hardened_runtime': True},
                         'pi': {'kind': 'debian-sha256'}, 'windows': {'kind': 'authenticode', 'certificate_sha1': 'd' * 40,
                             'timestamp': 'RFC3161-SHA256', 'verified': ['ori-core', 'ori-supervisor', 'ui', 'nsis']}}[platform]
                source = {'source_revision': REV, 'release_version': '0.4.0', 'manual_sha256': HASH,
                          'components': {name: {'sha256': HASH} for name in ('ori-core', 'ori-supervisor')}}
                bundles.freeze(platform, source, {'source_revision': REV, 'signing': proof, 'ui_sha256': HASH}, raw, candidate)
                row = dict(receipt(platform), artifact_sha256=bundles.digest(raw),
                           manifest_sha256=bundles.digest(candidate / f'{platform}-manifest.json'))
                receipt_path = root / (platform + '.json')
                receipt_path.write_text(json.dumps(row) + '\n')
                subprocess.run(['ssh-keygen', '-q', '-Y', 'sign', '-f', str(key), '-n',
                                gate.NAMESPACE + '/' + platform, str(receipt_path)], check=True)
                signed[platform] = {'ORI_RECEIPT_B64': base64.b64encode(receipt_path.read_bytes()).decode(),
                                    'ORI_SIGNATURE_B64': base64.b64encode(Path(str(receipt_path) + '.sig').read_bytes()).decode()}
                candidates[platform] = candidate

            class RepositoryFixture:
                revision = REV

                def run(self, run_id, workflow, platform=None):
                    if workflow == publish.BUILDER and run_id != 42:
                        raise ValueError('unexpected test build')

                def published(self, platform, number, destination):
                    tag = publish.platform_tag(platform, number)
                    if tag not in public:
                        raise ValueError('prerelease missing')
                    destination.mkdir()
                    for name, payload in public[tag].items():
                        (destination / name).write_bytes(payload)
                    bundles.verify_sums(destination, publish.platform_assets(platform))
                    return publish.accepted(self, destination, platform)

                def publish(self, folder, tag, prerelease):
                    self_test.assertNotIn(tag, public)
                    public[tag] = {file.name: file.read_bytes() for file in folder.iterdir()}

            self_test = self
            repo = RepositoryFixture()
            numbers = {platform: 1 for platform in candidates}

            def invoke(platform):
                with tempfile.TemporaryDirectory(dir=root) as outputs, patch.dict(os.environ, signed.get(platform, {})):
                    publish.promote(repo, platform, numbers, 42, candidates.get(platform), Path(outputs))

            with patch.dict(os.environ, {'ORI_WITNESS_' + platform.upper(): public_key for platform in candidates}):
                with self.assertRaisesRegex(ValueError, 'prerelease missing'):
                    invoke('windows')
                with self.assertRaisesRegex(ValueError, 'prerelease missing'):
                    invoke('stable')
                self.assertFalse(public)
                for platform in ('macos', 'pi', 'windows'):
                    invoke(platform)
                invoke('stable')
                for platform, filename in bundles.ARTIFACTS.items():
                    standalone = public[publish.platform_tag(platform, 1)]
                    self.assertEqual(public['v0.4.0'][filename], standalone[filename])
                    self.assertEqual(standalone[filename], (candidates[platform] / filename).read_bytes())

    def test_platform_publication_rejects_an_unexpected_remote_file_or_failed_asset_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.exe'
            path.write_bytes(b'controlled')
            good = {'name': path.name, 'state': 'uploaded', 'size': path.stat().st_size,
                    'digest': 'sha256:' + bundles.digest(path)}
            publish.verify_remote_assets({'assets': [good]}, {path.name: path})
            for changed in [{'digest': 'sha256:' + HASH}, {'size': 7}, {'state': 'starter'}, {'name': 'other.exe'}]:
                with self.assertRaises(ValueError):
                    publish.verify_remote_assets({'assets': [{**good, **changed}]}, {path.name: path})

    def test_blank_template_never_passes_and_guide_uses_only_exact_committed_blobs(self):
        from tools.release.bench_receipt import template
        from tools.release.operator_manual import stage_manual
        form = template('pi', REV, HASH, HASH, 42)
        with self.assertRaises(ValueError):
            gate.validate_receipt(form, 'pi', REV, HASH, HASH, 42)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = b'100644 blob cafe\tdocs/operator-guide/index.html\x00100644 blob face\tdocs/operator-guide/images/screen.png\x00'
            for i, src in enumerate(('./images/screen.png', 'https://example.invalid/remote.png', '../private.png')):
                content = {'cafe': f'<img src="{src}">'.encode(), 'face': b'controlled screenshot'}
                def git(args, cwd):
                    return listing if 'ls-tree' in args else content[args[-1]]
                with patch('tools.release.operator_manual.subprocess.check_output', side_effect=git):
                    destination = root / str(i)
                    if i:
                        with self.assertRaises(ValueError):
                            stage_manual(root, destination)
                    else:
                        self.assertTrue(bundles.sha(stage_manual(root, destination)))
                        self.assertEqual((destination / 'images/screen.png').read_bytes(), content['face'])

    def test_developer_id_signature_is_hardened_and_notary_must_return_an_accepted_uuid(self):
        from tools.release import developer_id
        with patch.object(developer_id, 'command') as run:
            developer_id.sign(Path('app'), 'A' * 40, runtime=True)
            args = run.call_args.args
            self.assertIn('--timestamp', args)
            self.assertIn('runtime', args)
            self.assertNotIn('-', args)
            for value in ({'status': 'Invalid', 'id': 'bd111af4-de74-4561-a520-25a490822af7'},
                          {'status': 'Accepted', 'id': 'broken'}):
                run.return_value = json.dumps(value)
                with self.assertRaises(ValueError):
                    developer_id.notarize(Path('app.zip'), 'test-profile')


if __name__ == '__main__':
    unittest.main()
