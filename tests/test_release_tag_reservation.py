"""Check create-only ref reservation and late ref changes without contacting a repository."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.release import publish_release as release


REV = 'a' * 40
TAG = 'linux-amd64-v0.4.0'


class ReleaseRefTests(unittest.TestCase):
    def repository(self):
        repo = release.Repository.__new__(release.Repository)
        repo.repo, repo.base, repo.revision = 'owner/repo', 'repos/owner/repo/', REV
        repo.immutable = Mock()
        return repo

    def test_native_publication_requires_a_human_approval_for_its_exact_environment(self):
        repo = self.repository()
        with patch.dict(os.environ, GITHUB_RUN_ID='123'), patch.object(release, 'github') as github:
            for state, user, environment in [('bypassed', {'type': 'User'}, 'release-publication'),
                                            ('rejected', {'type': 'User'}, 'release-publication'),
                                            ('approved', None, 'release-publication'),
                                            ('approved', {'type': 'Bot'}, 'release-publication'),
                                            ('approved', {'type': 'User'}, 'release-acceptance')]:
                with self.subTest(state=state, user=user, environment=environment):
                    repo.api = Mock(return_value=[dict(state=state, user=user, environments=[dict(name=environment)])])
                    with self.assertRaisesRegex(ValueError, 'recorded human review'):
                        repo.publish(Path('unused'), TAG, prerelease=False)
                    repo.api.assert_called_once_with('actions/runs/123/approvals')
                    repo.immutable.assert_not_called()
                    github.assert_not_called()

    def test_reservation_recovers_a_lost_response_only_if_exact_ref_exists(self):
        for after_create in (None, REV, 'b' * 40):
            with self.subTest(after_create=after_create):
                repo = self.repository()
                current = [None]
                repo.tag_ref = lambda tag, optional=False: {'object': {'sha': current[0]}} if current[0] else None
                def lost_response(path, data):
                    self.assertEqual(path, 'git/refs')
                    self.assertEqual(data, {'ref': 'refs/tags/' + TAG, 'sha': REV})
                    current[0] = after_create
                    raise ValueError('create response unavailable')
                repo.api = Mock(side_effect=lost_response)
                if after_create == REV:
                    repo.reserve_tag(TAG, REV)
                    repo.reserve_tag(TAG, REV)
                else:
                    with self.assertRaises(ValueError):
                        repo.reserve_tag(TAG, REV)
                self.assertEqual(repo.api.call_count, 1)
                self.assertEqual(current[0], after_create)

    def test_native_publication_never_patches_a_draft_when_reserved_tag_moves_during_upload(self):
        repo = self.repository()
        sha = [None]
        state = {}
        repo.tag_ref = lambda tag, optional=False: {'object': {'sha': sha[0]}} if sha[0] else None
        def repo_api(path, **options):
            if path == 'actions/runs/123/approvals':
                return [{'state': 'approved', 'user': {'type': 'User'}, 'environments': [{'name': 'release-publication'}]}]
            if path == 'git/refs':
                self.assertEqual(options['data'], {'ref': 'refs/tags/' + TAG, 'sha': REV})
                sha[0] = REV
                return
            self.assertEqual(path, 'releases/7')
            return state
        repo.api = repo_api
        def api(*args, data=None):
            if args[0] == '--paginate':
                return [[]]
            self.assertEqual(args[:2], ('-X', 'POST'))
            self.assertEqual(args[2], repo.base + 'releases')
            state.update(data, id=7, assets=[])
            return state
        def upload(args, check):
            self.assertEqual(args[:3], ['gh', 'release', 'upload'])
            path = Path(args[4])
            self.assertEqual(sha[0], REV)
            state['assets'].append(dict(name=path.name, size=path.stat().st_size,
                                        state='uploaded', digest='sha256:' + release.digest(path)))
            sha[0] = 'c' * 40
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GITHUB_RUN_ID='123'), \
                patch.object(release, 'github', side_effect=api), \
                patch.object(release.subprocess, 'run', side_effect=upload):
            folder = Path(tmp)
            (folder / 'SHA256SUMS').write_bytes(b'test')
            with self.assertRaisesRegex(ValueError, 'another commit'):
                repo.publish(folder, TAG, prerelease=False)
        self.assertTrue(state['draft'])
        self.assertEqual(sha[0], 'c' * 40)


if __name__ == '__main__':
    unittest.main()
