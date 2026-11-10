#!/usr/bin/env python3
"""Record package checksums and build-runtime notices; not a complete SBOM."""
import argparse
import hashlib
from importlib.metadata import distribution, PackageNotFoundError
import json
from pathlib import Path
import platform
import sys
import sysconfig
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--notices-only', action='store_true')
    parser.add_argument('--inventory-dir', type=Path, help='Hash the actual unpacked frozen files')
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.inventory_dir:
        args.inventory_dir = args.inventory_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    repository = Path(__file__).resolve().parents[2]
    source_sha = subprocess.check_output(['git','-C',str(repository),'rev-parse','HEAD'],text=True).strip()
    (directory/'LICENSE.txt').write_bytes((repository/'LICENSE').read_bytes())
    python_license = next((p for p in [Path(sysconfig.get_path('stdlib'))/'LICENSE.txt',
        Path(sys.base_prefix)/'LICENSE.txt', Path(sys.base_prefix)/'LICENSE'] if p.is_file()), None)
    if python_license is None:
        raise SystemExit('Build runtime Python LICENSE must be located before distributing it')
    (directory/'PYTHON-LICENSE.txt').write_bytes(python_license.read_bytes())
    for notice in (repository/'third_party').glob('pyserial-3.5-*'):
        (directory/notice.name).write_bytes(notice.read_bytes())
    components = []
    for name in ('openradioinspector', 'numpy', 'pyserial', 'sounddevice', 'cffi', 'pycparser'):
        try:package = distribution(name)
        except PackageNotFoundError:
            if name in ('sounddevice','cffi','pycparser'):continue
            raise
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
        'source_revision':source_sha, 'python':sys.version, 'platform':platform.platform(),
        'components':components,
        'limitations':'Inventory of selected installed runtime distributions. Verify actual frozen runtime, Python/Tcl/Tk, native libraries and build output separately before release.'
    },ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if args.inventory_dir:
        files = []
        manifest_path = directory/'frozen-files.json'
        for path in sorted(args.inventory_dir.rglob('*')):
            if path.is_file() and path != manifest_path:
                data = path.read_bytes()
                files.append({'path':path.relative_to(args.inventory_dir).as_posix(),
                              'size':len(data), 'sha256':hashlib.sha256(data).hexdigest()})
        manifest_path.write_text(json.dumps({'source_revision':source_sha, 'files':files},
                                           ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if args.notices_only:
        return
    paths = sorted(p for p in directory.iterdir() if p.is_file() and p.name != 'SHA256SUMS')
    (directory/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in paths),encoding='ascii')


if __name__ == '__main__':
    main()
