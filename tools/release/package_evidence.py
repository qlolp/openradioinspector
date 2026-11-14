#!/usr/bin/env python3
"""Record package checksums and build-runtime notices; not a complete SBOM."""
import argparse
import hashlib
from importlib.metadata import distribution, PackageNotFoundError
import json
from pathlib import Path
import platform
import stat
import sys
import sysconfig
import subprocess
import zipfile


MODERN_UI = 'radioinspector/web/modern/'
MAX_WHEEL_MEMBERS = 100000
MAX_UI_MANIFEST_BYTES = 1024 * 1024
MAX_UI_ASSET_BYTES = 32 * 1024 * 1024
MAX_UI_TOTAL_BYTES = 256 * 1024 * 1024
MAX_UI_FILES = 4096


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate modern UI manifest key')
        value[key] = item
    return value


def _local_path(name, *, directory=False):
    if not isinstance(name, str) or not name or len(name.encode('utf-8')) > 4096 \
            or any(character in name for character in ('\\', '\x00', ':', '?', '#')):
        raise ValueError('Unsafe modern UI wheel path')
    parts = (name[:-1] if directory else name).split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('Unsafe modern UI wheel path')


def validate_wheel_modern_ui(wheel):
    """Reject stale/missing generated assets before package evidence is written.

    Legacy wheels without modern UI files retain their previous behavior. This
    proves the generated UI file closure, not build provenance or a full SBOM.
    """
    try:
        with zipfile.ZipFile(wheel) as archive:
            entries = archive.infolist()
            modern = [entry for entry in entries if entry.filename.startswith(MODERN_UI) and not entry.is_dir()]
            if not modern:
                return False
            if len(entries) > MAX_WHEEL_MEMBERS or len(modern) > MAX_UI_FILES:
                raise ValueError('Modern UI wheel entry limit exceeded')
            names = set()
            for entry in entries:
                name = entry.filename
                if entry.orig_filename != name:
                    raise ValueError('Normalized modern UI wheel path')
                _local_path(name, directory=entry.is_dir())
                if name in names:
                    raise ValueError('Duplicate modern UI wheel entry')
                names.add(name)
                kind = stat.S_IFMT(entry.external_attr >> 16)
                allowed = (0, stat.S_IFDIR) if entry.is_dir() else (0, stat.S_IFREG)
                if kind not in allowed or entry.flag_bits & 1:
                    raise ValueError('Nonregular or encrypted modern UI wheel entry')
            manifest_path = MODERN_UI + 'modern-build.json'
            if manifest_path not in {entry.filename for entry in modern}:
                raise ValueError('Missing modern UI manifest')
            metadata = archive.getinfo(manifest_path)
            if metadata.file_size > MAX_UI_MANIFEST_BYTES:
                raise ValueError('Modern UI manifest byte limit exceeded')

            def invalid_constant(_value):
                raise ValueError('Nonfinite modern UI manifest value')

            value = json.loads(archive.read(metadata).decode('utf-8'),
                               object_pairs_hook=_unique_json_object, parse_constant=invalid_constant)
            if not isinstance(value, dict) or set(value) != {'version', 'offline', 'files'} \
                    or type(value['version']) is not int or value['version'] != 1 or value['offline'] is not True:
                raise ValueError('Invalid modern UI manifest schema')
            files = value['files']
            if not isinstance(files, list) or not 1 <= len(files) < MAX_UI_FILES:
                raise ValueError('Invalid modern UI manifest files')
            for name in files:
                _local_path(name)
            if len(set(files)) != len(files) or 'index.html' not in files or 'modern-build.json' in files:
                raise ValueError('Invalid modern UI index or duplicate manifest files')
            expected = {MODERN_UI + name for name in files} | {manifest_path}
            if {entry.filename for entry in modern} != expected:
                raise ValueError('Modern UI wheel files do not exactly match modern-build.json')
            total = 0
            for entry in modern:
                if entry.file_size > MAX_UI_ASSET_BYTES:
                    raise ValueError('Modern UI asset byte limit exceeded')
                total += entry.file_size
                if total > MAX_UI_TOTAL_BYTES:
                    raise ValueError('Modern UI total byte limit exceeded')
                # Read in bounded chunks to verify CRC/expansion without
                # extracting or retaining the full generated bundle in memory.
                count = 0
                with archive.open(entry) as source:
                    while chunk := source.read(1024 * 1024):
                        count += len(chunk)
                        if count > entry.file_size:
                            raise ValueError('Invalid modern UI expanded size')
                if count != entry.file_size:
                    raise ValueError('Invalid modern UI expanded size')
            return True
    except (zipfile.BadZipFile, UnicodeError, NotImplementedError, RuntimeError) as error:
        raise ValueError('Invalid modern UI wheel archive') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--notices-only', action='store_true')
    parser.add_argument('--inventory-dir', type=Path, help='Hash the actual unpacked frozen files')
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.inventory_dir:
        args.inventory_dir = args.inventory_dir.resolve()
    # Existing packaging CI calls this entry point before SHA256SUMS and upload.
    # Validate every wheel first; an invalid one must not receive new evidence.
    for wheel in sorted(directory.glob('*.whl')):
        validate_wheel_modern_ui(wheel)
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
