"""Check the actual app target, independently of SwiftPM's automatic discovery."""
from collections import Counter
from pathlib import Path
import plistlib
import unittest
import xml.etree.ElementTree as ET


class XcodeSourceRegistrationTests(unittest.TestCase):
    def test_each_native_source_is_compiled_once_and_object_keys_are_unique(self):
        root = Path(__file__).resolve().parents[1]
        project = root / 'macos/DvoynoyObzor.xcodeproj/project.pbxproj'
        for dictionary in ET.fromstring(project.read_bytes()).iter('dict'):
            keys = [child.text for child in dictionary if child.tag == 'key']
            self.assertEqual(len(keys), len(set(keys)), 'duplicate Xcode object/property key')
        objects = plistlib.loads(project.read_bytes())['objects']
        target = next(v for v in objects.values() if v.get('isa') == 'PBXNativeTarget')
        compiled = []
        for phase_id in target['buildPhases']:
            phase = objects[phase_id]
            if phase['isa'] != 'PBXSourcesBuildPhase':
                continue
            for build_id in phase['files']:
                reference = objects[objects[build_id]['fileRef']]
                if reference.get('path', '').endswith('.swift'):
                    compiled.append(reference['path'])
        expected = [p.name for p in (root / 'macos/DvoynoyObzor').glob('*.swift')]
        self.assertEqual(Counter(compiled), Counter(expected))
