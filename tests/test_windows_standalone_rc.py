"""Portable checks for RC staging. Native control/installation run on Windows."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.release import prepare_windows_node as release


class WindowsNodePackageTests(unittest.TestCase):
    def test_actual_bytes_revision_architecture_and_serial_feature_gate(self):
        revision = 'a' * 40
        with tempfile.TemporaryDirectory() as root:
            exe = Path(root) / 'ori-core.exe'
            exe.write_bytes(b'fixture')
            info = dict(schema_version=1, component='ori-core', source_revision=revision,
                        target_os='windows', target_arch='x86_64', features={'serial': True, 'audio': False})
            self.assertEqual(release.helper_record(exe, 'ori-core', revision, info)['sha256'], release.digest(exe))
            for change in ({'target_arch': 'aarch64'}, {'source_revision': 'b' * 40},
                           {'features': {'serial': False, 'audio': False}},
                           {'features': {'serial': True, 'audio': True}}, {'component': 'ori-supervisor'}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    release.helper_record(exe, 'ori-core', revision, {**info, **change})

    def test_tauri_resolves_the_complete_same_origin_production_build(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'dist'
            assets = source / 'assets'
            assets.mkdir(parents=True)
            (source / 'index.html').write_text('<script src="/modern/assets/app.js"></script>')
            (assets / 'app.js').write_text('import("/modern/assets/chunk.js")')
            (assets / 'chunk.js').write_text('export default 1')
            files = ['assets/app.js', 'assets/chunk.js', 'index.html']
            (source / 'modern-build.json').write_text(json.dumps({'version': 1, 'offline': True, 'files': files}))
            frontend = Path(root) / 'frontend'
            hashes = release.stage_web(source, frontend)
            self.assertEqual(hashes, {name: release.digest(frontend / 'modern' / name) for name in files})
            self.assertEqual((frontend / 'index.html').read_bytes(), (source / 'index.html').read_bytes())

    def test_rejects_untracked_and_gitignored_vite_and_core_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            (root / '.gitignore').write_text('*.csv\n.env.local\n')
            for folder in ('clients/web/src', 'clients/web/public', 'radioinspector/web/static'):
                (root / folder).mkdir(parents=True)
                (root / folder / 'tracked.js').write_text('export default 1')
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Package test', '-c', 'user.email=test@invalid',
                            'commit', '-qm', 'fixture'], cwd=root, check=True)
            release.verify_build_inputs(root)
            for name in ('clients/web/src/local.ts', 'clients/web/public/hidden.csv',
                         'radioinspector/web/static/hidden.csv', 'clients/web/.env.local'):
                with self.subTest(name=name):
                    local = root / name
                    local.write_text('fixture')
                    with self.assertRaises(ValueError):
                        release.verify_build_inputs(root)
                    local.unlink()

    def test_normal_tauri_build_packages_a_current_user_offline_node(self):
        config = json.loads((release.WINDOWS / 'tauri.conf.json').read_text())
        self.assertIn('prepare_windows_node.py', config['build']['beforeBuildCommand'])
        self.assertEqual(config['version'], release.VERSION)
        self.assertEqual(config['bundle']['resources'], {'runtime': 'runtime'})
        self.assertEqual(config['bundle']['targets'], ['nsis'])
        windows = config['bundle']['windows']
        self.assertEqual(windows['webviewInstallMode']['type'], 'offlineInstaller')
        self.assertEqual(windows['nsis']['installMode'], 'currentUser')
        self.assertTrue((release.WINDOWS / windows['nsis']['installerHooks']).is_file())
