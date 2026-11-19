#!/usr/bin/env python3
"""Collect notices for the locked native binaries and the shared production WebUI."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def cargo_metadata(manifest, target, features=()):
    return json.loads(subprocess.check_output([
        'cargo', 'metadata', '--locked', '--format-version', '1', '--manifest-path', str(manifest),
        '--filter-platform', target, *features,
    ], cwd=ROOT))


def selected_packages(metadata, roots):
    nodes = {node['id']: node for node in metadata['resolve']['nodes']}
    packages = {package['id']: package for package in metadata['packages']}
    pending = [key for key in metadata['workspace_members'] if packages[key]['name'] in roots]
    if {packages[key]['name'] for key in pending} != set(roots):
        raise ValueError('Missing a selected native Cargo package')
    selected = set()
    while pending:
        key = pending.pop()
        if key in selected:
            continue
        selected.add(key)
        for dep in nodes[key]['deps']:
            if any(kind['kind'] != 'dev' for kind in dep['dep_kinds']):
                pending.append(dep['pkg'])
    return [packages[key] for key in sorted(selected)]


def copy_notices(source, destination, output, declared=None):
    # Include nested notices for bundled C libraries as well as top-level license files.
    candidates = {path for path in source.rglob('*') if path.is_file()
                  and path.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'COPYRIGHT', 'NOTICE', 'UNLICENSE'))}
    if declared:
        candidates.add(source / declared)
    names = []
    for path in sorted(candidates):
        if not path.resolve(strict=True).is_relative_to(source.resolve()) or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError(f'Invalid notice in {source.name}: {path.name}')
        target = destination / path.relative_to(source)
        target.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(path, target)
        names.append(target.relative_to(output).as_posix())
    return names


def collect(output, target, tauri=False, metadata=None):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ori-notices-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'notices'
        staged.mkdir()
        write_notices(staged, target, tauri, metadata)
        staged.rename(output)


def write_notices(output, target, tauri, metadata):
    shutil.copyfile(ROOT / 'LICENSE', output / 'OpenRadioInspector-LICENSE.txt')
    shutil.copyfile(ROOT / 'Cargo.lock', output / 'Cargo.lock')
    metadata = metadata or cargo_metadata(ROOT / 'Cargo.toml', target, ['--no-default-features', '--features', 'ori-core/serial'])
    selected = selected_packages(metadata, {'ori-core', 'ori-runtime'})
    if tauri:
        app = ROOT / 'clients/windows'
        shutil.copyfile(app / 'Cargo.lock', output / 'Tauri-Cargo.lock')
        selected.extend(selected_packages(cargo_metadata(app / 'Cargo.toml', target), {'openradioinspector-windows'}))
    crates = {}
    for package in selected:
        name, version = package['name'], package['version']
        source = package['source'] or 'OpenRadioInspector'
        key = name, version, source
        if key in crates:
            continue
        if package['source'] is None:
            files = ['OpenRadioInspector-LICENSE.txt']
        else:
            files = copy_notices(Path(package['manifest_path']).parent, output / 'cargo' / f'{name}-{version}',
                                 output, package['license_file'])
        crates[key] = dict(name=name, version=version, source=source,
                           license=package['license'] or ('MIT' if package['source'] is None else None),
                           notice_files=files)

    web = ROOT / 'clients/web'
    shutil.copyfile(web / 'package-lock.json', output / 'package-lock.json')
    npm = []
    for relative, item in sorted(json.loads((web / 'package-lock.json').read_text(encoding='utf-8'))['packages'].items()):
        if not relative or item.get('dev'):
            continue
        directory = web / relative
        description = json.loads((directory / 'package.json').read_text(encoding='utf-8'))
        if description['version'] != item['version']:
            raise ValueError(f'Installed npm package does not match lock: {relative}')
        files = copy_notices(directory, output / 'npm' / relative.removeprefix('node_modules/'), output)
        npm.append(dict(name=description['name'], version=item['version'], license=description.get('license'),
                        integrity=item.get('integrity'), notice_files=files))
    sysroot = Path(subprocess.check_output(['rustc', '--print', 'sysroot'], text=True).strip())
    for name in ['COPYRIGHT-library.html', 'COPYRIGHT.html']:
        shutil.copyfile(sysroot / 'share/doc/rust' / name, output / ('Rust-' + name))
    write_json(output / 'licenses.json', {
        'schema_version': 1, 'target': target,
        'scope': 'Cargo normal and build dependency closure for the selected target; npm production lock and Rust toolchain notices.',
        'cargo': [crates[key] for key in sorted(crates)], 'npm': npm,
    })


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tauri', action='store_true', help='Include the Windows desktop Cargo workspace')
    args = parser.parse_args()
    collect(args.output, args.target, args.tauri)
