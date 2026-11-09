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
        reference=next(k for k,v in project.items() if v.get('path')=='TinySAControlPane.swift')
        build=next(k for k,v in project.items() if v.get('isa')=='PBXBuildFile' and v.get('fileRef')==reference)
        self.assertTrue(any(build in v.get('files',[]) for v in project.values() if v.get('isa')=='PBXSourcesBuildPhase'))
