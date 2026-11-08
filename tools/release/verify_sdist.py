#!/usr/bin/env python3
"""Check actual source archives contain native build inputs and empty packages."""
import sys
import tarfile

required = {
    'macos/DvoynoyObzor.xcodeproj/project.pbxproj',
    'macos/DvoynoyObzor/ContentView.swift',
    'macos/SpektrKomandy/Package.swift',
    'macos/Package.swift',
    'packaging/macos/build.sh',
    'radioinspector/web/static/platform.js',
    'radioinspector/windows/__init__.py',
}
with tarfile.open(sys.argv[1]) as archive:
    names = {m.name.split('/',1)[1] for m in archive if '/' in m.name}
missing = required-names
if missing:
    raise SystemExit('Missing source build inputs: '+', '.join(sorted(missing)))
print('Source build inputs present')
