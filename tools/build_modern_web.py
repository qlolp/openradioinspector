"""Package an already built, offline Vite client into the Python distribution."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def package(source, destination):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if not (source/'index.html').is_file():
        raise ValueError('Build clients/web with npm run build first')
    files = sorted(path for path in source.rglob('*') if not path.is_dir())
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError('Client build must contain only regular files')
    if destination.exists():
        if destination.is_symlink() or not (destination/'modern-build.json').is_file():
            raise ValueError('Refusing to replace an unrelated asset directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ori-web-build-', dir=destination.parent) as temporary:
        staged = Path(temporary)/'assets'
        staged.mkdir()
        for path in files:
            output = staged/path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path,output)
        (staged/'modern-build.json').write_text(json.dumps({'version':1,'offline':True,
            'files':[path.relative_to(source).as_posix() for path in files]},indent=2)+'\n',encoding='utf-8')
        old = Path(temporary)/'previous'
        if destination.exists():
            destination.rename(old)
        try:
            staged.rename(destination)
        except BaseException:
            if old.exists():old.rename(destination)
            raise
    return destination


def check(destination):
    """Verify a completed package without rebuilding or changing its files."""
    destination = Path(destination).absolute()
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError('Packaged modern UI directory is missing')
    if not (destination/'index.html').is_file() or not (destination/'modern-build.json').is_file():
        raise ValueError('Packaged modern UI needs index.html and modern-build.json')
    entries = list(destination.rglob('*'))
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in entries):
        raise ValueError('Packaged modern UI must contain regular files and directories')
    manifest = json.loads((destination/'modern-build.json').read_text(encoding='utf-8'))
    files = sorted(path.relative_to(destination).as_posix() for path in entries
                   if path.is_file() and path != destination/'modern-build.json')
    if (not isinstance(manifest, dict) or manifest.get('version') != 1
            or manifest.get('offline') is not True or manifest.get('files') != files):
        raise ValueError('Packaged modern UI manifest and complete file list differ')
    return destination


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'clients/web/dist')
    parser.add_argument('--destination',type=Path,default=ROOT/'radioinspector/web/modern')
    parser.add_argument('--check',action='store_true',help='Read-only check of the already packaged UI')
    args=parser.parse_args()
    if args.check:
        try:
            destination = check(args.destination)
        except (OSError, ValueError) as error:
            parser.exit(2, f'Modern UI inputs are unavailable or incomplete: {error}\n'
                        'Prepare them before compiling Core:\n'
                        '  npm --prefix clients/web run build\n'
                        '  python tools/build_modern_web.py\n')
        print(destination)
    else:
        print(package(args.source,args.destination))


if __name__ == '__main__':main()
