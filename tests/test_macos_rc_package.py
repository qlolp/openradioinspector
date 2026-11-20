import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.release import macos_rc_package as package


class MacOSRCPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'packaging/macos/build_personal_rc.py'
        spec = importlib.util.spec_from_file_location('personal_rc_builder_fixture', path)
        cls.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.builder)

    def test_busy_image_creation_never_promotes_a_partial_dmg(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'candidate.dmg'
            paths = []

            def create(args, **kwargs):
                scratch = Path(args[-1])
                self.assertFalse(scratch.exists())
                self.assertFalse(target.exists())
                paths.append(scratch)
                if len(paths) == 1:
                    scratch.write_bytes(b'partial')
                    return subprocess.CompletedProcess(args, 1, 'hdiutil: create failed - Resource busy\n')
                scratch.write_bytes(b'complete image')
                return subprocess.CompletedProcess(args, 0, 'created\n')

            with patch.object(self.builder.subprocess, 'run', side_effect=create), \
                 patch.object(self.builder, 'run') as verify, patch('time.sleep') as pause:
                self.builder.create_disk_image(Path(directory), target)
                self.assertEqual(len(set(paths)), 2)
                verify.assert_called_once_with('hdiutil', 'verify', paths[1])
                pause.assert_called_once()
                self.assertEqual(target.read_bytes(), b'complete image')
                with self.assertRaises(FileExistsError):
                    self.builder.create_disk_image(Path(directory), target)
                self.assertEqual(len(paths), 2)

    def test_dmg_creation_fails_without_masking_corruption_or_exhausted_busy_device(self):
        for reason, attempts in [('Resource busy', 3), ('Permission denied', 1), (None, 1)]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'candidate.dmg'
                def create(args, **kwargs):
                    Path(args[-1]).write_bytes(b'invalid image')
                    return subprocess.CompletedProcess(args, int(reason is not None),
                        f'hdiutil: create failed - {reason}\n' if reason else '')
                with patch.object(self.builder.subprocess, 'run', side_effect=create) as calls, \
                     patch.object(self.builder, 'run', side_effect=subprocess.CalledProcessError(1, ['verify'])) as verify, \
                     patch('time.sleep') as pause:
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.builder.create_disk_image(Path(directory), target)
                    self.assertEqual(calls.call_count, attempts)
                    self.assertEqual(pause.call_count, attempts - 1)
                    self.assertEqual(verify.call_count, int(reason is None))
                    self.assertFalse(target.exists())

    def app(self, root):
        app = root / 'Relocated RC.app'
        (app / 'Contents/MacOS').mkdir(parents=True)
        (app / 'Contents/Helpers').mkdir()
        for name in ['MacOS/OpenRadioInspectorRC', 'Helpers/ori-core', 'Helpers/ori-supervisor']:
            path = app / 'Contents' / name
            path.write_bytes(b'owned synthetic executable\x00' * 4096)
            path.chmod(0o755)
        return app

    def test_inventory_survives_relocation_and_detects_missing_changed_and_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self.app(root)
            inventory = package.inventory(app)
            moved = root / 'another location' / app.name
            moved.parent.mkdir(); app.rename(moved)
            package.verify_inventory(moved, inventory)
            path = moved / 'Contents/Helpers/ori-core'
            original = path.read_bytes()
            path.write_bytes(original + b'tampered')
            with self.assertRaises(ValueError): package.verify_inventory(moved, inventory)
            path.write_bytes(original); path.chmod(0o755)
            extra = moved / 'Contents/extra'; extra.write_text('unexpected')
            with self.assertRaises(ValueError): package.verify_inventory(moved, inventory)
            extra.unlink(); path.unlink()
            with self.assertRaises(ValueError): package.verify_inventory(moved, inventory)

    def test_external_symlink_and_legacy_python_payload_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); app = self.app(root)
            (root / 'outside').write_text('not a bundled resource')
            link = app / 'Contents/escape'; link.symlink_to(root / 'outside')
            with self.assertRaises(ValueError): package.inventory(app)
            link.unlink()
            (app / 'Contents/Helpers/Python.framework').mkdir()
            with self.assertRaises(ValueError): package.validate_runtime_profile(app)

    def test_notices_are_created_when_xcode_app_has_no_resources_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            destination = folder/'RC.app/Contents/Resources/Notices'
            crate = folder/'crate'; crate.mkdir()
            (crate/'Cargo.toml').write_text('[package]\nname="ori-core"\nversion="0.1.0"\n')
            (crate/'LICENSE').write_text('Controlled fixture license')
            docs = folder/'sysroot/share/doc/rust'; docs.mkdir(parents=True)
            for name in ['COPYRIGHT-library.html', 'COPYRIGHT.html']: (docs/name).write_text('Controlled Rust notice fixture')
            metadata = {'packages': [dict(id='fixture-core', name='ori-core', version='0.1.0', license='MIT', source=None,
                manifest_path=str(crate/'Cargo.toml'))], 'resolve': {'nodes': [dict(id='fixture-core', dependencies=[])]}}
            self.builder.notices(destination, metadata, folder/'sysroot')
            self.assertEqual((destination/'crates/ori-core-0.1.0/LICENSE').read_text(), 'Controlled fixture license')
            inventory = json.loads((destination/'cargo-build-dependency-closure.json').read_text())
            self.assertEqual(inventory[0]['name'], 'ori-core')
            self.assertTrue((destination/'OpenRadioInspector-MIT.txt').is_file())

    def test_component_profile_rejects_mixed_source_architecture_and_audio(self):
        source = 'a' * 40
        core = dict(component='ori-core', source_revision=source, target_arch='aarch64', target_os='macos',
                    features={'serial': True, 'audio': False}, sqlite_version='3.54.1', version='0.1.0')
        supervisor = dict(component='ori-supervisor', source_revision=source, target_arch='aarch64', target_os='macos', version='0.1.0')
        package.validate_components(core, supervisor, source)
        for changed in [dict(core, target_arch='x86_64'), dict(core, target_os='linux'), dict(core, source_revision='b' * 40),
                        dict(core, features={'serial': True, 'audio': True})]:
            with self.assertRaises(ValueError): package.validate_components(changed, supervisor, source)


if __name__ == '__main__':
    unittest.main()
