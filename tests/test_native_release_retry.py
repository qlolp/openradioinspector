"""Exercise empty and partially uploaded native drafts through the ordinary promotion path."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_signed_release import HASH, NATIVE_CI, REV, receipt
from tools.release import publish_release as publish, release_artifacts as bundles


PLATFORM = bundles.LINUX_AMD64
TAG = bundles.platform_tag(PLATFORM, 2)
PROOF = PLATFORM + '-acceptance.proof'


class NativeDraftRetryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.candidate = self.root / 'candidate'
        package = self.root / bundles.ARTIFACTS[PLATFORM]
        package.write_bytes(b'frozen offline DEB fixture')
        source = dict(source_revision=REV, release_version='0.4.0', manual_sha256=HASH,
                      components={name: dict(sha256=HASH) for name in ('ori-core', 'ori-supervisor')})
        evidence = dict(source_revision=REV, signing=dict(kind='debian-sha256'), ui_sha256=HASH, native_ci=NATIVE_CI)
        bundles.freeze(PLATFORM, source, evidence, package, self.candidate)
        self.receipt = json.dumps(receipt(PLATFORM, bundles.digest(package),
                                          bundles.digest(self.candidate / (PLATFORM + '-manifest.json')))).encode()
        self.original = dict(subject=hashlib.sha256(self.receipt).hexdigest(), issued=1)
        self.renewed = dict(self.original, issued=2)
        self.bundles = [self.original]
        self.state, self.blobs, self.tag, self.interrupt_after = {}, {}, None, 0
        self.downloads, self.publishes = 0, 0
        self.repo = publish.Repository.__new__(publish.Repository)
        self.repo.repo, self.repo.base, self.repo.revision = 'owner/repo', 'repos/owner/repo/', REV
        self.repo.immutable, self.repo.run, self.repo.api = Mock(), Mock(), self.api
        for ctx in (patch.object(publish, 'github', side_effect=self.github),
                    patch.object(publish.subprocess, 'run', side_effect=self.gh),
                    patch.object(publish, 'verify_github_attestation', side_effect=self.verify), patch('builtins.print')):
            ctx.start()
            self.addCleanup(ctx.stop)

    def api(self, path, **options):
        if path == 'actions/runs/123/approvals':
            return [dict(state='approved', user=dict(type='User'), environments=[dict(name='release-publication')])]
        if path == 'git/ref/tags/' + TAG:
            return {'object': {'type': 'commit', 'sha': self.tag}} if self.tag else None
        if path == 'git/refs':
            self.assertIsNone(self.tag)
            self.assertEqual(options['data'], {'ref': 'refs/tags/' + TAG, 'sha': REV})
            self.tag = REV
            return
        self.assertEqual(path, 'releases/4')
        return self.state

    def github(self, *args, data=None, output=None):
        if args[0] == '--paginate':
            return [[self.state]] if self.state else [[]]
        if args[:2] == ('-H', 'Accept: application/octet-stream'):
            asset = next(a for a in self.state['assets'] if a['name'] == PROOF)
            self.assertEqual(args[2], self.repo.base + f"releases/assets/{asset['id']}")
            output.write(self.blobs[PROOF])
            return
        if args[:2] == ('-X', 'DELETE'):
            self.assertTrue(self.state['draft'])
            self.assertEqual(args[2], self.repo.base + 'releases/assets/591')
            asset = next(a for a in self.state['assets'] if a['id'] == 591)
            self.assertTrue(publish.empty_upload(asset))
            self.assertNotIn(asset['name'], self.blobs)
            self.state['assets'].remove(asset)
            return
        if args[:2] == ('-X', 'POST'):
            self.assertFalse(self.state)
            self.state.update(data, id=4, assets=[])
            return self.state
        self.assertEqual(args, ('-X', 'PATCH', self.repo.base + 'releases/4'))
        self.assertEqual(data, dict(draft=False, make_latest='false'))
        self.assertEqual(self.tag, REV)
        self.publishes += 1
        self.state.update(draft=False, immutable=True, html_url='https://example.invalid/release')
        return self.state

    def gh(self, args, **options):
        if args[:3] == ['gh', 'attestation', 'download']:
            self.downloads += 1
            (Path(options['cwd']) / 'bundles.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.bundles))
            return
        self.assertEqual(args[:4], ['gh', 'release', 'upload', TAG])
        if len(self.blobs) == self.interrupt_after:
            raise RuntimeError('interrupted fixture upload')
        path = Path(args[4])
        self.assertNotIn(path.name, self.blobs)
        self.blobs[path.name] = path.read_bytes()
        self.state['assets'].append(dict(id=len(self.blobs), name=path.name, size=path.stat().st_size,
                                         state='uploaded', digest='sha256:' + bundles.digest(path)))

    def verify(self, repo, subject, proof):
        self.assertIs(repo, self.repo)
        bundle = json.loads(proof.read_bytes())
        if bundle not in (self.original, self.renewed) or bundle['subject'] != bundles.digest(subject):
            raise ValueError('unverified test attestation')

    def run_promotion(self, receipt_bytes=None):
        encoded = base64.b64encode(self.receipt if receipt_bytes is None else receipt_bytes).decode()
        with tempfile.TemporaryDirectory(dir=self.root) as working, patch.dict(os.environ,
                 GITHUB_RUN_ID='123', ORI_RECEIPT_B64=encoded, ORI_SIGNATURE_B64=''):
            publish.promote(self.repo, PLATFORM, {PLATFORM: 2}, 42, self.candidate, Path(working))

    def interrupt(self, uploaded_count):
        self.interrupt_after = uploaded_count
        with self.assertRaisesRegex(RuntimeError, 'interrupted fixture upload'):
            self.run_promotion()
        self.assertTrue(self.state['draft'])
        self.assertEqual(len(self.blobs), uploaded_count)
        self.interrupt_after = None

    def assert_original_release(self, body):
        self.assertEqual(self.state['body'], body)
        self.assertTrue(self.state['immutable'])
        self.assertEqual(self.publishes, 1)
        self.assertEqual(list(self.blobs)[0], PROOF)
        self.assertEqual(json.loads(self.blobs[PROOF]), self.original)
        self.assertEqual(self.blobs[PLATFORM + '-acceptance.json'], self.receipt)
        self.assertEqual(body, publish.release_body(REV, hashlib.sha256(self.blobs['SHA256SUMS']).hexdigest()))

    def test_empty_draft_selects_only_the_verified_previously_reserved_bundle(self):
        self.interrupt(0)
        body = self.state['body']
        self.bundles = [self.renewed]
        with self.assertRaisesRegex(ValueError, 'matches the SHA256SUMS'):
            self.run_promotion()
        self.assertFalse(self.blobs)
        self.assertEqual(self.publishes, 0)
        self.bundles.append(self.original)
        # Even the original proof cannot be mixed with a changed receipt.
        with self.assertRaisesRegex(ValueError, 'matches the SHA256SUMS'):
            self.run_promotion(self.receipt + b' ')
        self.assertFalse(self.blobs)
        self.run_promotion()
        self.assert_original_release(body)

    def test_empty_starter_proof_after_502_recovers_only_the_reserved_bundle(self):
        self.interrupt(0)
        body = self.state['body']
        self.state['assets'].append(dict(id=591, state='starter', size=0, name=PROOF, digest=None))
        self.bundles = [self.renewed]
        with self.assertRaisesRegex(ValueError, 'matches the SHA256SUMS'):
            self.run_promotion()
        self.assertEqual(self.state['assets'][0]['id'], 591)
        self.bundles.append(self.original)
        self.run_promotion()
        self.assert_original_release(body)

    def test_only_an_empty_placeholder_on_the_same_draft_can_be_removed(self):
        self.interrupt(0)
        path = self.root / PROOF
        path.write_bytes(b'completed original bytes')
        paths = {PROOF: path}
        asset = dict(name=PROOF, id=591, state='starter', size=0, digest=None)
        for bad in [dict(asset, size=3), dict(asset, digest='sha256:' + HASH), dict(asset, state='uploaded')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                publish.existing_assets(dict(self.state, assets=[bad]), paths)
        with self.assertRaises(ValueError):
            publish.existing_assets(dict(self.state, draft=False, assets=[asset]), paths)
        for live in [dict(self.state, draft=False), dict(self.state, target_commitish='f' * 40),
                     dict(self.state, assets=[dict(asset, id=99)]), dict(self.state, assets=[dict(asset, size=1)])]:
            with self.subTest(live=live), patch.object(self.repo, 'api', return_value=live), self.assertRaises(ValueError):
                publish.recover_empty_uploads(self.repo, self.state, [asset], paths)
        self.state['assets'] = [dict(asset, state='uploaded', size=path.stat().st_size, digest='sha256:' + bundles.digest(path))]
        self.assertEqual(publish.recover_empty_uploads(self.repo, self.state, [asset], paths), {PROOF})

    def test_partially_uploaded_and_published_releases_reverify_stored_bytes(self):
        self.interrupt(2)
        self.assertIn(PROOF, self.blobs)
        body = self.state['body']
        self.bundles = []
        with self.assertRaisesRegex(ValueError, 'differs from reserved SHA256SUMS'):
            self.run_promotion(self.receipt + b' ')
        with patch.object(publish, 'verify_github_attestation', side_effect=ValueError('invalid stored witness')):
            with self.assertRaisesRegex(ValueError, 'invalid stored witness'):
                self.run_promotion()
        self.assertEqual(len(self.blobs), 2)
        self.assertEqual(self.publishes, 0)
        self.run_promotion()
        self.run_promotion()
        self.assertEqual(self.downloads, 1)
        self.assert_original_release(body)


if __name__ == '__main__':
    unittest.main()
