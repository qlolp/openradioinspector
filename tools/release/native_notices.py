#!/usr/bin/env python3
"""Collect notices for the locked native binaries and the shared production WebUI."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
# These build dependencies also supply code in the distributed JavaScript.
# Vite emits the modulepreload polyfill and Rolldown emits CommonJS helpers.
WEB_RUNTIME_NOTICES = {
    'node_modules/vite': ('LICENSE.md',),
    'node_modules/rolldown': ('LICENSE', 'THIRD-PARTY-LICENSE'),
}
# The crates.io asn1-rs-impl 0.2.0 archive points at the impl/ directory of
# this commit. Its two license files live at the upstream workspace root.
ASN1_IMPL_LICENSE_ORIGIN = 'https://github.com/rusticata/asn1-rs/tree/a20e5f7319c896737ad0f2557037817b91ad854f'
ASN1_IMPL_LICENSES = {
    'LICENSE-APACHE': 'a60eea817514531668d7e00765731449fe14d059d3249e0bc93b36de45f759f2',
    'LICENSE-MIT': 'a5c61b93b6ee1d104af9920cf020ff3c7efe818e31fe562c72261847a728f513',
}


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


def copy_notices(source, destination, output, *declared, max_bytes=16 * 1024 * 1024):
    # Include nested notices for bundled C libraries as well as top-level license files.
    candidates = {path for path in source.rglob('*') if path.is_file()
                  and path.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'COPYRIGHT', 'NOTICE', 'UNLICENSE'))}
    candidates.update(source / name for name in declared if name)
    names = []
    for path in sorted(candidates):
        if not path.resolve(strict=True).is_relative_to(source.resolve()) or path.stat().st_size > max_bytes:
            raise ValueError(f'Invalid notice in {source.name}: {path.name}')
        target = destination / path.relative_to(source)
        target.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(path, target)
        names.append(target.relative_to(output).as_posix())
    return names


def link_asn1_impl_licenses(crates, output):
    registry = 'registry+https://github.com/rust-lang/crates.io-index'
    proc_macro = crates.get(('asn1-rs-impl', '0.2.0', registry))
    if proc_macro is None:
        return
    matches = {}
    for (name, _, source), crate in crates.items():
        if (name, source) != ('asn1-rs', registry):
            continue
        for relative in crate['notice_files']:
            filename = Path(relative).name
            expected = ASN1_IMPL_LICENSES.get(filename)
            if expected and hashlib.sha256((output / relative).read_bytes()).hexdigest() == expected:
                matches[filename] = relative
    if matches.keys() != ASN1_IMPL_LICENSES.keys():
        raise ValueError('Missing exact upstream workspace licenses for asn1-rs-impl 0.2.0')
    proc_macro['notice_files'] = sorted(matches.values())
    proc_macro['notice_origin'] = ASN1_IMPL_LICENSE_ORIGIN


def web_notices(web, output):
    lock = web / 'package-lock.json'
    shutil.copyfile(lock, output / 'package-lock.json')
    packages = json.loads(lock.read_text(encoding='utf-8'))['packages']
    if WEB_RUNTIME_NOTICES.keys() - packages.keys():
        raise ValueError('Expected Vite and Rolldown runtime dependencies in the WebUI lockfile')
    npm = []
    for relative, item in sorted(packages.items()):
        if not relative or (item.get('dev') and relative not in WEB_RUNTIME_NOTICES):
            continue
        directory = web / relative
        description = json.loads((directory / 'package.json').read_text(encoding='utf-8'))
        if description['version'] != item['version']:
            raise ValueError(f'Installed npm package does not match lock: {relative}')
        destination = output / 'npm' / relative.removeprefix('node_modules/')
        files = copy_notices(directory, destination, output, *WEB_RUNTIME_NOTICES.get(relative, ()))
        npm.append(dict(name=description['name'], version=item['version'], license=description.get('license'),
                        integrity=item.get('integrity'), notice_files=files))
    return npm


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

    link_asn1_impl_licenses(crates, output)
    npm = web_notices(ROOT / 'clients/web', output)
    sysroot = Path(subprocess.check_output(['rustc', '--print', 'sysroot'], text=True).strip())
    for name in ['COPYRIGHT-library.html', 'COPYRIGHT.html']:
        shutil.copyfile(sysroot / 'share/doc/rust' / name, output / ('Rust-' + name))
    write_json(output / 'licenses.json', {
        'schema_version': 1, 'target': target,
        'scope': 'Cargo normal and build dependencies for the selected target, production npm packages, embedded Vite and Rolldown runtime helpers, and Rust toolchain notices.',
        'cargo': [crates[key] for key in sorted(crates)], 'npm': npm,
    })


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tauri', action='store_true', help='Include the Windows desktop Cargo workspace')
    args = parser.parse_args()
    collect(args.output, args.target, args.tauri)
