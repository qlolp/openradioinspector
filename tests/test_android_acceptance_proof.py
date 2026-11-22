"""Exercise Field's human-witnessed artifact through local publication and retry fixtures."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_android_distribution import PIN, UPLOAD, sample_receipt
from tools.field import play_distribution as release
from tools.release import publish_release as shared
from tools.release.macos_rc_package import digest, write_json


class AndroidWitnessTests(unittest.TestCase):
    def test_verifier_pins_the_subject_workflow_branch_and_review_commit(self):
        repo = SimpleNamespace(repo='owner/project', revision='b' * 40)
        with patch.object(shared.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as command:
            with self.assertRaisesRegex(ValueError, 'no valid GitHub attestation'):
                shared.verify_github_attestation(repo, Path(release.RECEIPT), Path(release.PROOF),
                                                 workflow='android-field-release.yml', branches=('main',))
            args = command.call_args.args[0]
            self.assertEqual(command.call_count, 1)
            self.assertEqual(args[:6], ['gh', 'attestation', 'verify', release.RECEIPT, '--bundle', release.PROOF])
            for option, value in [('--repo', repo.repo), ('--signer-workflow', 'owner/project/.github/workflows/android-field-release.yml'),
                                  ('--signer-digest', repo.revision), ('--source-digest', repo.revision),
                                  ('--source-ref', 'refs/heads/main')]:
                self.assertEqual(args[args.index(option) + 1], value)
            self.assertIn('--deny-self-hosted-runners', args)

    def test_missing_or_invalid_proof_cannot_create_a_draft_and_reruns_reuse_the_verified_first_proof(self):
        repo, play = shared.Repository.__new__(shared.Repository), Mock()
        repo.repo, repo.base, repo.revision = 'owner/project', 'repos/owner/project/', 'b' * 40
        repo.immutable = Mock()
        build, receipt = sample_receipt()
        build.update(version_name='0.1.0', review_revision=repo.revision, application_id=release.PACKAGE,
                     play_cert_sha256=PIN, upload_cert_sha256=UPLOAD)
        state = {}
        contents = {}
        uploads = []
        tag_sha = [None]
        move_during_upload = [True]
        approval = [{'state': 'approved', 'user': {'type': 'User'}, 'environments': [{'name': 'android-field-publication'}]}]
        def repo_api(path, **options):
            if path == 'actions/runs/123/approvals':
                return approval
            if path == 'git/refs':
                self.assertIsNone(tag_sha[0])
                self.assertEqual(options['data'], {'ref': 'refs/tags/android-field-v0.1.0', 'sha': build['source_revision']})
                tag_sha[0] = options['data']['sha']
                return
            return state
        repo.api = repo_api
        repo.tag_ref = lambda tag, optional=False: {'object': {'type': 'commit', 'sha': tag_sha[0]}} if tag_sha[0] else None

        def api(*args, data=None, output=None):
            if args[0] == '--paginate':
                return [[state]] if state else [[]]
            if args[:2] == ('-X', 'POST'):
                self.assertFalse(state)
                state.update(data, id=7, assets=[])
                return state
            if args[:2] == ('-X', 'PATCH'):
                self.assertEqual(tag_sha[0], build['source_revision'])
                self.assertEqual(data, {'draft': False, 'make_latest': 'false'})
                state.update(draft=False, immutable=True, html_url='https://example.invalid/tag')
                return state
            self.assertEqual(args[:2], ('-H', 'Accept: application/octet-stream'))
            proof_id = next(a['id'] for a in state['assets'] if a['name'] == release.PROOF)
            self.assertEqual(args[2], repo.base + f'releases/assets/{proof_id}')
            output.write(contents[release.PROOF])

        def upload(args, **kwargs):
            self.assertEqual(args[:3], ['gh', 'release', 'upload'])
            path = Path(args[4])
            self.assertNotIn(path.name, contents)
            contents[path.name] = path.read_bytes()
            state['assets'].append(dict(name=path.name, size=path.stat().st_size, id=len(contents),
                                        state='uploaded', digest='sha256:' + digest(path)))
            uploads.append(path.name)
            if move_during_upload[0]:
                tag_sha[0] = 'f' * 40

        def verify(repo_, receipt_file, proof_file, **constraints):
            self.assertIs(repo_, repo)
            self.assertEqual(json.loads(receipt_file.read_bytes()), receipt)
            self.assertEqual(constraints, dict(workflow='android-field-release.yml', branches=('main',)))
            if proof_file.read_bytes() != b'proof of this reviewed receipt':
                raise ValueError('unverified proof')

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GITHUB_RUN_ID='123',
                FIELD_PLAY_CERT_SHA256=PIN, FIELD_UPLOAD_CERT_SHA256=UPLOAD), patch.object(release, 'github', side_effect=api), \
                patch.object(release.subprocess, 'run', side_effect=upload), patch.object(release, 'verify_github_attestation', side_effect=verify), \
                patch('builtins.print'):
            root = Path(tmp)

            def publish(number, bundle):
                out = root / str(number)
                out.mkdir()
                apk = out / release.apk_name(build)
                apk.write_bytes(b'Play universal fixture')
                receipt['apk_sha256'] = digest(apk)
                write_json(out / release.RECEIPT, receipt)
                build.update(apk=apk.name, apk_bytes=apk.stat().st_size, apk_sha256=digest(apk),
                             acceptance_sha256=digest(out / release.RECEIPT))
                write_json(out / release.RECORD, build)
                release.write_sums(out)
                proof = None
                if bundle is not None:
                    proof = root / f'{number}.sigstore'
                    proof.write_bytes(bundle)
                release.publish(repo, out, digest(out / 'SHA256SUMS'), play, proof)

            with self.assertRaisesRegex(ValueError, 'attestation is required'):
                publish(1, None)
            with self.assertRaisesRegex(ValueError, 'unverified proof'):
                publish(2, b'signature from a different workflow')
            self.assertFalse(state)
            with self.assertRaisesRegex(ValueError, 'another commit'):
                publish(3, b'proof of this reviewed receipt')
            self.assertTrue(state['draft'])
            self.assertNotIn('immutable', state)
            # A later attempt can resume only once the fixture ref again matches.
            move_during_upload[0] = False
            tag_sha[0] = build['source_revision']
            publish(4, b'different bundle issued on retry')
            self.assertTrue(state['immutable'])
            self.assertEqual(uploads[0], release.PROOF)
            self.assertEqual(set(contents), {release.PROOF, release.RECEIPT, release.RECORD, release.apk_name(build), 'SHA256SUMS'})
            self.assertIn(release.PROOF.encode(), contents['SHA256SUMS'])
            publish(5, b'different bundle issued on retry')
            self.assertEqual(uploads.count(release.PROOF), 1)
            self.assertEqual(contents[release.PROOF], b'proof of this reviewed receipt')


if __name__ == '__main__':
    unittest.main()
