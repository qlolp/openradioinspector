import json
from pathlib import Path
import tempfile
import unittest
import plistlib
import os
import stat
from unittest import mock

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

    def target_app(self, root):
        """A synthetic Mac filesystem, including executable bits absent on NTFS.

        Only these three fixture paths receive target attributes. Missing files,
        symlinks and every other bundle retain the real host filesystem checks.
        No synthetic executable is ever launched.
        """
        app = self.app(root)
        executables = {app / 'Contents' / name for name in
            ['MacOS/OpenRadioInspectorRC', 'Helpers/ori-core', 'Helpers/ori-supervisor']}
        actual_stat = Path.stat
        def target_stat(path, *args, **kwargs):
            result = actual_stat(path, *args, **kwargs)
            if path in executables:
                return os.stat_result((result.st_mode | stat.S_IXUSR, *result[1:]))
            return result
        patcher = mock.patch.object(Path, 'stat', autospec=True, side_effect=target_stat)
        patcher.start()
        self.addCleanup(patcher.stop)
        return app

    def test_target_fixture_does_not_relax_permissions_of_another_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.target_app(root / 'target')
            package.validate_runtime_profile(target)
            other = self.app(root / 'other')
            (other / 'Contents/MacOS/OpenRadioInspectorRC').chmod(0o644)
            with self.assertRaises(ValueError): package.validate_runtime_profile(other)

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

    def test_audio_candidate_is_explicit_and_cannot_masquerade_as_rc1(self):
        source='a'*40
        core=dict(component='ori-core',source_revision=source,target_arch='aarch64',target_os='macos',
            features={'serial':True,'audio':True},sqlite_version='3.54.1',version='0.1.0')
        supervisor=dict(component='ori-supervisor',source_revision=source,target_arch='aarch64',target_os='macos',version='0.1.0')
        package.validate_components(core,supervisor,source,profile='rc2-audio')
        with self.assertRaises(ValueError): package.validate_components(core,supervisor,source)
        with self.assertRaises(ValueError):
            package.validate_components(dict(core,features={'serial':True,'audio':False}),supervisor,source,profile='rc2-audio')
        with tempfile.TemporaryDirectory() as directory:
            app=self.target_app(Path(directory))
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'fixture'}))
            build=package.prepare(app,source,core,supervisor,{},profile='rc2-audio')
            info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
            self.assertEqual(build['release_version'],'0.4.0-macos.rc.2')
            self.assertEqual(build['hardware_validation'],'not_run')
            self.assertTrue(info['ORINativeAudioCapture'])
            self.assertTrue(info['NSMicrophoneUsageDescription'])
            self.assertNotIn('native audio capture',build['excluded_components'])
            self.assertIn('DuckDB worker',build['excluded_components'])
            manifest=Path(directory)/'manifest.json'; package.evidence(app,manifest)
            self.assertEqual(json.loads(manifest.read_text())['release_version'],'0.4.0-macos.rc.2')

    def test_default_rc1_never_declares_or_requests_microphone_capture(self):
        source='a'*40
        core=dict(component='ori-core',source_revision=source,target_arch='aarch64',target_os='macos',
            features={'serial':True,'audio':False},sqlite_version='3.54.1',version='0.1.0')
        supervisor=dict(component='ori-supervisor',source_revision=source,target_arch='aarch64',target_os='macos',version='0.1.0')
        with tempfile.TemporaryDirectory() as directory:
            app=self.target_app(Path(directory))
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'NSMicrophoneUsageDescription':'must be removed'}))
            build=package.prepare(app,source,core,supervisor,{})
            info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
            self.assertEqual(build['release_version'],package.RELEASE_VERSION)
            self.assertFalse(info.get('ORINativeAudioCapture',False))
            self.assertNotIn('NSMicrophoneUsageDescription',info)
            info.pop('ORINativeAudioCapture',None)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
            package.evidence(app,Path(directory)/'legacy-rc1-manifest.json')

    def test_evidence_rejects_ui_flags_which_disagree_with_the_admitted_profile(self):
        source='a'*40
        core=dict(component='ori-core',source_revision=source,target_arch='aarch64',target_os='macos',
            features={'serial':True,'audio':True},sqlite_version='3.54.1',version='0.1.0')
        supervisor=dict(component='ori-supervisor',source_revision=source,target_arch='aarch64',target_os='macos',version='0.1.0')
        with tempfile.TemporaryDirectory() as directory:
            app=self.target_app(Path(directory));info_path=app/'Contents/Info.plist'
            info_path.write_bytes(plistlib.dumps({}))
            package.prepare(app,source,core,supervisor,{},profile='rc2-audio')
            original=plistlib.loads(info_path.read_bytes())
            cases=[('ORINativeAudioCapture',False),('NSMicrophoneUsageDescription',None),
                ('ORIReleaseVersion','0.4.0-macos.rc.1'),('ORIPersonalRC',False),
                ('CFBundleExecutable','unexpected'),('CFBundleIdentifier','unexpected')]
            for index,(field,value) in enumerate(cases):
                with self.subTest(field=field):
                    info=dict(original)
                    if value is None: info.pop(field,None)
                    else: info[field]=value
                    info_path.write_bytes(plistlib.dumps(info))
                    output=Path(directory)/f'invalid-{index}.json'
                    with self.assertRaises(ValueError): package.evidence(app,output)
                    self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
