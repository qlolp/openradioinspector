#!/usr/bin/env python3
"""Record package checksums and build-runtime notices; not a complete SBOM."""
import argparse
import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
import platform
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(parents=True, exist_ok=True)
    components = []
    for name in ('openradioinspector', 'numpy', 'pyserial'):
        package = distribution(name)
        notices = []
        for item in package.files or ():
            if '.dist-info/' in str(item) and any(part in item.name.lower() for part in ('license', 'copying', 'notice')):
                path = package.locate_file(item)
                if path.is_file():
                    notices.append({'file':str(item), 'text':path.read_text(encoding='utf-8', errors='replace')})
        components.append({'name':name, 'version':package.version,
                           'license':package.metadata.get('License-Expression') or package.metadata.get('License'),
                           'notices':notices})
    (directory/'runtime-notices.json').write_text(json.dumps({
        'python':sys.version, 'platform':platform.platform(),
        'components':components,
        'limitations':'Inventory of selected installed runtime distributions. Verify actual frozen runtime, Python/Tcl/Tk, native libraries and build output separately before release.'
    },ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    paths = sorted(p for p in directory.iterdir() if p.is_file() and p.name != 'SHA256SUMS')
    (directory/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in paths),encoding='ascii')


if __name__ == '__main__':
    main()
