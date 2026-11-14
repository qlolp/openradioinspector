"""Check source ownership across the Xcode launcher and linked local package."""
from collections import Counter
from pathlib import Path
import plistlib
import re
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
        # Xcode compiles the entry point; the local Swift package owns app UI.
        self.assertEqual(compiled, ['DvoynoyObzorApp.swift'])
        products = [objects[key] for key in target['packageProductDependencies']]
        self.assertEqual([item['productName'] for item in products], ['DvoynoyObzorSession'])
        self.assertEqual(objects[products[0]['package']]['relativePath'], '.')
        linked = [objects[key].get('productRef') for phase_id in target['buildPhases']
                  if objects[phase_id]['isa'] == 'PBXFrameworksBuildPhase'
                  for key in objects[phase_id]['files']]
        self.assertIn(target['packageProductDependencies'][0], linked)
        manifest = (root / 'macos/Package.swift').read_text()
        implementation = re.search(r'\.target\(name: "DvoynoyObzorSession",(.*?)swiftSettings:', manifest, re.S)
        self.assertIsNotNone(implementation)
        self.assertIn('path: "DvoynoyObzor"', implementation.group(1))
        excluded = re.search(r'exclude: \[(.*?)\]', implementation.group(1)).group(1)
        exclusions = re.findall(r'"([^"]+)"', excluded)
        self.assertEqual(exclusions, compiled)
        expected = [p.name for p in (root / 'macos/DvoynoyObzor').glob('*.swift')]
        package_sources = [name for name in expected if name not in exclusions]
        self.assertIn('TinySAControlPane.swift', package_sources)
        self.assertEqual(Counter(compiled + package_sources), Counter(expected))
