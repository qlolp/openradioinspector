import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.release import native_notices

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pi_rc_package', ROOT / 'packaging/pi/build_rc.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)
ELF = """Class: ELF64
  Data: 2's complement, little endian
  Machine: AArch64
  [Requesting program interpreter: /lib/ld-linux-aarch64.so.1]
  (NEEDED) Shared library: [libc.so.6]
  Name: GLIBC_2.34
"""


class PiReleaseTests(unittest.TestCase):
    def test_build_rejects_untracked_or_ignored_assets_and_keeps_build_outputs_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            (root / '.gitignore').write_text('*.csv\n.env.local\nnode_modules/\n')
            subprocess.run(['git', 'add', '.gitignore'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                            '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'], cwd=root, check=True)
            for relative in ['clients/web/node_modules/build/input.js', 'clients/web/dist/assets/generated.js',
                             'tools/release/__pycache__/native_notices.pyc']:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'generated')
            self.assertEqual(len(package.revision(root)), 40)
            for relative in ['clients/web/public/untracked.css', 'clients/web/public/ignored.csv',
                             'clients/web/public/node_modules/app.js', 'clients/web/.env.local',
                             'clients/web/src/ignored.csv', 'radioinspector/web/static/unknown.js',
                             'crates/ori-extra/src/lib.rs', '.cargo/config.toml',
                             'packaging/debian/templates/extra.csv', 'docs/debian-amd64.md']:
                with self.subTest(relative=relative):
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'untracked')
                    with self.assertRaisesRegex(ValueError, 'Untracked build input'):
                        package.revision(root)
                    path.unlink()

    def test_installer_rejects_another_abi_or_host_development_path(self):
        with patch.object(package, 'capture', return_value=ELF):
            abi = package.inspect_elf(Path('ori-core'))
            self.assertEqual(abi['highest_required_glibc_symbol'], '2.34')
        for output in (ELF.replace('AArch64', 'X86-64'), ELF.replace('ELF64', 'ELF32'),
                       ELF.replace('2.34', '2.38'), ELF.replace('libc.so.6', 'libpython.so'),
                       ELF + '(RUNPATH) [/opt/build]\n', ELF.replace('/lib/ld-linux-aarch64', '/opt/out/ld-linux-aarch64')):
            with self.subTest(output=output), patch.object(package, 'capture', return_value=output), self.assertRaises(ValueError):
                package.inspect_elf(Path('ori-core'))

    def test_amd64_uses_its_own_interpreter_libraries_and_debian_12_symbol_ceiling(self):
        amd64 = ELF.replace('AArch64', 'Advanced Micro Devices X86-64').replace(
            '/lib/ld-linux-aarch64.so.1', '/lib64/ld-linux-x86-64.so.2')
        relr = amd64 + '  Name: GLIBC_ABI_DT_RELR\n'
        with patch.object(package, 'capture', return_value=relr):
            self.assertEqual(package.inspect_elf(Path('core'), package.AMD64)['highest_required_glibc_symbol'], '2.36')
            with self.assertRaises(ValueError):
                package.inspect_elf(Path('core'), package.PI)
        for invalid in (ELF, amd64.replace('/lib64/', '/usr/local/lib64/'), amd64.replace('2.34', '2.37'),
                        amd64.replace('little endian', 'big endian'), amd64.replace('ELF64', 'ELF32'),
                        amd64 + ' (NEEDED) Shared library: [ld-linux-aarch64.so.1]',
                        amd64 + ' Name: GLIBC_PRIVATE', amd64 + ' (RPATH) [/opt/lib]'):
            with self.subTest(invalid=invalid), patch.object(package, 'capture', return_value=invalid):
                with self.assertRaisesRegex(ValueError, 'amd64 Debian 12'):
                    package.inspect_elf(Path('core'), package.AMD64)

    def test_amd64_rejects_crossed_build_identity(self):
        source = 'b' * 40
        core = dict(component='ori-core', target_arch='x86_64', target_os='linux', version='0.1.0',
                    source_revision=source, features={'serial': True, 'audio': False}, sqlite_version='3.54.1')
        for field, wrong in [('target_arch', 'aarch64'), ('target_os', 'windows'), ('source_revision', 'a' * 40),
                             ('component', 'ori-supervisor'), ('features', {'serial': False, 'audio': False})]:
            with self.subTest(field=field), patch.object(package, 'inspect_elf', return_value={}), \
                    patch.object(package, 'capture', return_value=json.dumps(dict(core, **{field: wrong}))):
                with self.assertRaises(ValueError):
                    package.inventory_components(Path('bin'), source, package.AMD64)

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'requires Debian package tools')
    def test_both_packages_stage_their_own_unit_config_and_maintainer_scripts(self):
        profiles = [(package.PI, 'openradioinspector-pi-rc', 'ori-pi-rc', 'arm64'),
                    (package.AMD64, 'openradioinspector-linux', 'ori-linux', 'amd64')]
        for profile, name, user, arch in profiles:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = root / 'stage'
                package.stage_service(stage, profile)
                doc = stage / 'usr/share/doc' / name
                doc.mkdir(parents=True)
                (doc / 'README.md').write_text('test fixture')
                products = root / 'out'
                info = dict(release_version='0.4.0', target=profile.target, package=name, debian_architecture=arch)
                package.assemble(stage, products, 1, os.environ.copy(), info, profile)
                deb = products / f'{name}_0.4.0-1_{arch}.deb'
                self.assertEqual({p.name for p in products.iterdir()}, {deb.name, 'SHA256SUMS', 'release.json', 'README.md'})
                for line in (products / 'SHA256SUMS').read_text().splitlines():
                    sha, file = line.split('  ')
                    self.assertEqual(hashlib.sha256((products / file).read_bytes()).hexdigest(), sha)
                for field, expected in [('Package', name), ('Architecture', arch), ('Version', '0.4.0-1')]:
                    self.assertEqual(subprocess.check_output(['dpkg-deb', '-f', deb, field], text=True).strip(), expected)
                dependencies = subprocess.check_output(['dpkg-deb', '-f', deb, 'Depends'], text=True).strip()
                self.assertIn('ca-certificates', dependencies.split(', '))
                installed = root / 'extracted'
                subprocess.run(['dpkg-deb', '-R', deb, installed], check=True)
                unit = (installed / 'usr/lib/systemd/system' / f'{name}.service').read_text()
                self.assertIn(f'User={user}\nGroup={user}\nSupplementaryGroups=dialout\n', unit)
                self.assertIn(f'StateDirectory={name}\nStateDirectoryMode=0700\n', unit)
                self.assertIn(f'/usr/lib/{name}/ori-supervisor run /etc/{name}/supervisor.json', unit)
                self.assertIn(f'u {user} ', (installed / 'usr/lib/sysusers.d' / f'{name}.conf').read_text())
                config = json.loads((installed / profile.config).read_text())
                self.assertEqual(config['data_directory'], '/var/lib/' + name)
                self.assertEqual(config['backend']['program'], f'/usr/lib/{name}/ori-core')
                self.assertIn('--allow-serial', config['backend']['args'])
                self.assertEqual(config['backend']['control_addr'], '127.0.0.1:8770')
                self.assertEqual((installed / 'DEBIAN/conffiles').read_text(), f'/etc/{name}/supervisor.json\n')
                for script in ('postinst', 'prerm', 'postrm'):
                    maintainer = installed / 'DEBIAN' / script
                    self.assertIn(f'{name}.service', maintainer.read_text())
                    self.assertTrue(os.access(maintainer, os.X_OK))
                for script in ('prepare-archive', 'wait-ready', 'check-install'):
                    path = installed / profile.lib / script
                    self.assertTrue(os.access(path, os.X_OK))
                    self.assertIn(f'/var/lib/{name}', path.read_text())
                self.assertEqual((installed / profile.doc / 'release.json').read_bytes(),
                                 (products / 'release.json').read_bytes())
                self.assertEqual(json.loads((products / 'release.json').read_text()), dict(info, debian_version='0.4.0-1'))

    def test_release_refuses_a_different_statically_linked_sqlite_engine(self):
        source = 'a' * 40
        component = dict(component='ori-core', target_arch='aarch64', target_os='linux', version='0.1.0',
                         source_revision=source, features={'serial': True, 'audio': False}, sqlite_version='3.54.0')
        with patch.object(package, 'inspect_elf', return_value={}), patch.object(package, 'capture', return_value=json.dumps(component)):
            with self.assertRaisesRegex(ValueError, 'bundled SQLite 3.54.1'):
                package.inventory_components(Path('bin'), source)

    def test_cargo_notices_follow_the_selected_dependency_kinds(self):
        metadata = {'workspace_members': ['core', 'other'], 'packages': [
            {'id': key, 'name': key} for key in ['core', 'other', 'dep', 'builder', 'dev']],
            'resolve': {'nodes': [{'id': 'core', 'deps': [
                {'pkg': 'dep', 'dep_kinds': [{'kind': None}]},
                {'pkg': 'builder', 'dep_kinds': [{'kind': 'build'}]},
                {'pkg': 'dev', 'dep_kinds': [{'kind': 'dev'}]}]},
                *[{'id': key, 'deps': []} for key in ['other', 'dep', 'builder', 'dev']]]}}
        self.assertEqual([p['id'] for p in native_notices.selected_packages(metadata, {'core'})], ['builder', 'core', 'dep'])
        with self.assertRaises(ValueError):
            native_notices.selected_packages(metadata, {'missing'})

    def test_incomplete_notice_collection_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'bundle' / 'notices'
            with patch.object(native_notices, 'write_notices', side_effect=ValueError('missing input')), self.assertRaises(ValueError):
                native_notices.collect(output, 'aarch64-unknown-linux-gnu')
            self.assertFalse(output.exists())
            with patch.object(native_notices, 'write_notices') as write:
                native_notices.collect(output, 'aarch64-unknown-linux-gnu')
                write.assert_called_once()
            with self.assertRaises(FileExistsError):
                native_notices.collect(output, 'aarch64-unknown-linux-gnu')

    def test_notices_include_the_vite_polyfill_and_rolldown_helpers_embedded_in_the_webui(self):
        with tempfile.TemporaryDirectory() as directory:
            web = Path(directory) / 'web'
            output = Path(directory) / 'out'
            output.mkdir()
            packages = {'': {}, 'node_modules/vitest': {'version': '1', 'dev': True}}
            for name, licenses in [('react', ['LICENSE']), ('vite', ['LICENSE.md']),
                                   ('rolldown', ['LICENSE', 'THIRD-PARTY-LICENSE'])]:
                relative = f'node_modules/{name}'
                packages[relative] = dict(version='1', dev=name != 'react', integrity='test-integrity')
                installed = web / relative
                installed.mkdir(parents=True)
                (installed / 'package.json').write_text(json.dumps(dict(name=name, version='1', license='MIT')))
                for license_file in licenses:
                    (installed / license_file).write_text(f'{name} {license_file}')
            (web / 'package-lock.json').write_text(json.dumps({'packages': packages}))
            notices = {entry['name']: entry for entry in native_notices.web_notices(web, output)}
            self.assertEqual(notices.keys(), {'react', 'vite', 'rolldown'})
            self.assertEqual(notices['vite']['notice_files'], ['npm/vite/LICENSE.md'])
            self.assertEqual(notices['rolldown']['notice_files'], ['npm/rolldown/LICENSE', 'npm/rolldown/THIRD-PARTY-LICENSE'])
            for name, entry in notices.items():
                self.assertEqual(entry['integrity'], 'test-integrity')
                for file in entry['notice_files']:
                    self.assertEqual((output / file).read_text(), f'{name} {Path(file).name}')
            (web / 'node_modules/rolldown/THIRD-PARTY-LICENSE').unlink()
            with self.assertRaises(FileNotFoundError):
                native_notices.web_notices(web, output)

    def test_notices_include_nested_native_files_and_reject_an_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'crate'
            vendor = source / 'vendor'
            vendor.mkdir(parents=True)
            (vendor / 'COPYRIGHT').write_text('redistribution terms')
            destination = root / 'notices'
            self.assertEqual(native_notices.copy_notices(source, destination / 'crate', destination),
                             ['crate/vendor/COPYRIGHT'])
            (root / 'outside').write_text('private')
            try:
                (source / 'LICENSE').symlink_to(root / 'outside')
            except OSError:
                self.skipTest('symlink creation requires system permission')
            with self.assertRaises(ValueError):
                native_notices.copy_notices(source, destination / 'crate', destination)


if __name__ == '__main__':
    unittest.main()
