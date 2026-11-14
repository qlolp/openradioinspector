import shutil
import subprocess
import unittest
from pathlib import Path

class WebTinySATests(unittest.TestCase):
    def test_real_handlers_reject_stale_results_and_release_held_touch(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([shutil.which('node') or 'node',str(root/'tests/webui_tinysa.cjs'),str(root/'radioinspector/web/static/instrument.js')],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_macos_build_and_package_include_shared_surface(self):
        root=Path(__file__).resolve().parents[1]
        import plistlib
        project=plistlib.loads((root/'macos/DvoynoyObzor.xcodeproj/project.pbxproj').read_bytes())['objects']
        from tests.test_xcode_sources import XcodeSourceRegistrationTests
        # The package owns this pane; verify the actual linked source ownership.
        XcodeSourceRegistrationTests().test_each_native_source_is_compiled_once_and_object_keys_are_unique()
        self.assertTrue((root/'macos/DvoynoyObzor/TinySAControlPane.swift').is_file())
        app_configs=[v['buildSettings'] for v in project.values() if v.get('isa')=='XCBuildConfiguration' and v.get('buildSettings',{}).get('PRODUCT_BUNDLE_IDENTIFIER')]
        self.assertEqual(len(app_configs),2)
        for config in app_configs:self.assertIn('аналогового',config['INFOPLIST_KEY_NSMicrophoneUsageDescription'])
