"""Real ZIP closure checks; no UI build, package install, device, or release."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile

from tools.release import package_evidence

PREFIX = 'radioinspector/web/modern/'


class WheelModernUI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ori-wheel-ui-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def wheel(self, *, files=None, manifest=None, entries=()):
        if files is None:
            files = {'index.html': b'<script src="/modern/assets/app.js"></script>',
                     'assets/app.js': b'console.log("local fixture")'}
        if manifest is None:
            manifest = json.dumps({'version': 1, 'offline': True, 'files': sorted(files)}).encode()
        path = self.root / 'fixture.whl'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(PREFIX + 'modern-build.json', manifest)
                for name, body in files.items():
                    archive.writestr(PREFIX + name, body)
                for name, body in entries:
                    archive.writestr(name, body)
        return path

    def validate(self, wheel):
        return package_evidence.validate_wheel_modern_ui(wheel)

    def test_evidence_rejects_orphan_before_notices_or_checksums(self):
        wheel = self.wheel(entries=[(PREFIX + 'assets/old.js', b'orphan')])
        output = self.root / 'dist'
        output.mkdir()
        shutil.copy2(wheel, output / wheel.name)
        stdlib = self.root / 'stdlib'
        stdlib.mkdir()
        (stdlib / 'LICENSE.txt').write_text('Synthetic Python-license provider for this test')
        selected_notice = SimpleNamespace(version='fixture', metadata={'License': 'MIT'}, files=[])
        # Only unrelated selected-notice providers are synthetic. The actual
        # package-evidence entry point processes the malformed real ZIP.
        with patch.object(sys, 'argv', ['package_evidence.py', str(output)]), \
                patch.object(package_evidence.sysconfig, 'get_path', return_value=str(stdlib)), \
                patch.object(package_evidence, 'distribution', return_value=selected_notice):
            with self.assertRaises(ValueError):
                package_evidence.main()
        self.assertFalse((output / 'SHA256SUMS').exists())
        self.assertFalse((output / 'runtime-notices.json').exists())

    def test_exact_local_ui_and_legacy_without_modern_are_accepted(self):
        self.assertTrue(self.validate(self.wheel()))
        legacy = self.root / 'legacy.whl'
        with zipfile.ZipFile(legacy, 'w') as archive:
            archive.writestr('radioinspector/web/static/index.html', b'legacy unchanged')
        self.assertFalse(self.validate(legacy))

    def test_missing_orphan_and_missing_index_fail(self):
        for files, declared in [({'index.html': b'index'}, ['index.html', 'missing.js']),
                                ({'index.html': b'index', 'old.js': b'old'}, ['index.html']),
                                ({'app.js': b'app'}, ['app.js'])]:
            with self.subTest(files=files, declared=declared):
                manifest = json.dumps({'version': 1, 'offline': True, 'files': declared}).encode()
                with self.assertRaises(ValueError):
                    self.validate(self.wheel(files=files, manifest=manifest))

    def test_manifest_shape_version_duplicates_and_nonfinite_json_fail(self):
        for manifest in [b'not JSON', b'{"version":1,"version":1,"offline":true,"files":["index.html"]}',
                         b'{"version":true,"offline":true,"files":["index.html"]}',
                         b'{"version":1,"offline":false,"files":["index.html"]}',
                         b'{"version":1,"offline":true,"files":["index.html","index.html"]}',
                         b'{"version":1,"offline":true,"files":["index.html"],"extra":NaN}',
                         b'{"version":2,"offline":true,"files":["index.html"]}',
                         b'{"version":1,"offline":true,"files":["index.html","modern-build.json"]}']:
            with self.subTest(manifest=manifest):
                with self.assertRaises(ValueError):
                    self.validate(self.wheel(manifest=manifest))

    def test_zip_duplicates_and_unsafe_member_paths_fail(self):
        for name in [PREFIX + 'index.html', PREFIX + '../escape.js', PREFIX + 'assets/../app.js',
                     '/absolute.js', 'C:/absolute.js', PREFIX + 'assets\\app.js',
                     PREFIX + './app.js', PREFIX + 'assets//app.js']:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.validate(self.wheel(entries=[(name, b'unsafe')]))

    def test_manifest_paths_are_exact_safe_relative_paths(self):
        for name in ['../escape.js', '/absolute.js', 'https://example.invalid/app.js',
                     '//example.invalid/app.js', 'assets\\app.js', './index.html',
                     'assets//app.js', 'assets/app.js?query', 'assets/app.js#fragment']:
            with self.subTest(name=name):
                manifest = json.dumps({'version': 1, 'offline': True, 'files': ['index.html', name]}).encode()
                with self.assertRaises(ValueError):
                    self.validate(self.wheel(manifest=manifest))

    def test_symlink_and_special_ui_members_fail(self):
        for file_type in (stat.S_IFLNK, stat.S_IFIFO):
            wheel = self.wheel()
            with zipfile.ZipFile(wheel, 'a') as archive:
                member = zipfile.ZipInfo(PREFIX + 'link')
                member.create_system = 3
                member.external_attr = (file_type | 0o777) << 16
                archive.writestr(member, 'index.html')
            with self.subTest(file_type=file_type), self.assertRaises(ValueError):
                self.validate(wheel)

    def test_bounds_apply_to_metadata_and_expanded_assets_without_large_fixture(self):
        wheel = self.wheel()
        for constant, limit in [('MAX_UI_MANIFEST_BYTES', 8), ('MAX_UI_ASSET_BYTES', 4),
                                ('MAX_UI_TOTAL_BYTES', 16), ('MAX_WHEEL_MEMBERS', 1)]:
            with self.subTest(constant=constant), patch.object(package_evidence, constant, limit):
                with self.assertRaises(ValueError):
                    self.validate(wheel)


if __name__ == '__main__':
    unittest.main()
