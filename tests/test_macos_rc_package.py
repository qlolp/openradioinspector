import json
from pathlib import Path
import tempfile
import unittest

from tools.release import macos_rc_package as package


class MacOSRCPackageTests(unittest.TestCase):
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
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('personal_rc_builder_fixture', root/'packaging/macos/build_personal_rc.py')
        builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
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
            builder.notices(destination, metadata, folder/'sysroot')
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
