import importlib.util
from pathlib import Path
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
    def test_installer_rejects_another_abi_or_host_development_path(self):
        with patch.object(package, 'capture', return_value=ELF):
            abi = package.inspect_elf(Path('ori-core'))
            self.assertEqual(abi['highest_required_glibc_symbol'], '2.34')
        for output in (ELF.replace('AArch64', 'X86-64'), ELF.replace('ELF64', 'ELF32'),
                       ELF.replace('2.34', '2.38'), ELF.replace('libc.so.6', 'libpython.so'),
                       ELF + '(RUNPATH) [/opt/build]\n', ELF.replace('/lib/ld-linux-aarch64', '/opt/out/ld-linux-aarch64')):
            with self.subTest(output=output), patch.object(package, 'capture', return_value=output), self.assertRaises(ValueError):
                package.inspect_elf(Path('ori-core'))

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
