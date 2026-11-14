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
