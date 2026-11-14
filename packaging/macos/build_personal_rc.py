#!/usr/bin/env python3
"""Build the offline arm64 personal RC; Python is a build tool only."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/release'))
import macos_rc_package as package


def run(*args, env=None, capture=False):
    print('+', ' '.join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], cwd=ROOT, env=env, check=True,
                          text=True, stdout=subprocess.PIPE if capture else None).stdout


def notices(destination, metadata, sysroot):
    destination.mkdir()
    shutil.copyfile(ROOT / 'LICENSE', destination / 'OpenRadioInspector-MIT.txt')
    shutil.copyfile(ROOT / 'Cargo.lock', destination / 'Cargo.lock')
    # This is the complete Cargo dependency closure for both selected roots,
    # including build dependencies. It is intentionally not called a runtime SBOM.
    nodes = {n['id']: n for n in metadata['resolve']['nodes']}
    packages = {p['id']: p for p in metadata['packages']}
    pending = [p['id'] for p in packages.values() if p['name'] in {'ori-core', 'ori-runtime'}]
    selected = set()
    while pending:
        key = pending.pop()
        if key in selected: continue
        selected.add(key); pending.extend(nodes[key]['dependencies'])
    entries = []
    for key in sorted(selected):
        item = packages[key]
        base = Path(item['manifest_path']).parent.resolve()
        label = item['name'] + '-' + item['version']
        files = []
        for source in sorted(base.iterdir()):
            if source.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')) and source.is_file():
                if not source.resolve().is_relative_to(base) or source.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError('unsafe license file: ' + label)
                target = destination / 'crates' / label / source.name
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, target)
                files.append(str(target.relative_to(destination)))
        entries.append(dict(name=item['name'], version=item['version'], license=item['license'],
                            source=item['source'] or 'OpenRadioInspector workspace', notice_files=files))
    for name in ['COPYRIGHT-library.html', 'COPYRIGHT.html']:
        source = sysroot / 'share/doc/rust' / name
        if not source.is_file(): raise ValueError('Rust distribution copyright file missing')
        shutil.copyfile(source, destination / ('Rust-' + name))
    package.write_json(destination / 'cargo-build-dependency-closure.json', entries)
    (destination / 'README.txt').write_text(
        'Native SwiftUI + Rust personal RC. No Python interpreter, DuckDB, PortAudio or Node runtime is bundled.\n'
        'System Apple frameworks/Swift runtime are provided by macOS. Cargo entries include build dependencies; '
        'the list is not a claim of an exact runtime SBOM. The lock file and Rust library copyright accompany it.\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'build/macos/personal-rc')
    args = parser.parse_args()
    if platform.system() != 'Darwin': raise SystemExit('Personal RC packaging requires macOS and Xcode; no artifact was built.')
    if run('git', 'status', '--porcelain', '--untracked-files=no', capture=True).strip():
        raise SystemExit('Commit tracked source changes before packaging.')
    source = run('git', 'rev-parse', 'HEAD', capture=True).strip()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    packages_dir = output / 'packages'; packages_dir.mkdir()
    stage = output / 'dmg-root'; stage.mkdir()
    env = os.environ.copy()
    env.update(ORI_BUILD_SOURCE_SHA=source, CARGO_TARGET_DIR=str(output / 'cargo'),
               MACOSX_DEPLOYMENT_TARGET='14.0', RUSTUP_TOOLCHAIN='1.99.1')
    rust = run('rustc', '--version', env=env, capture=True).strip()
    if not rust.startswith('rustc 1.99.1 '): raise ValueError('Rust 1.99.1 required')
    swift = run('xcrun', 'swift', '--version', capture=True).strip()
    xcode = run('xcodebuild', '-version', capture=True).strip()
    metadata = json.loads(run('cargo', 'metadata', '--locked', '--format-version', '1', '--filter-platform',
                             'aarch64-apple-darwin', '--features', 'ori-core/serial', env=env, capture=True))
    run('cargo', 'build', '--locked', '--release', '--target', 'aarch64-apple-darwin',
        '-p', 'ori-core', '-p', 'ori-runtime', '--no-default-features', '--features', 'ori-core/serial', env=env)
    derived = output / 'DerivedData'
    run('xcodebuild', '-project', 'macos/DvoynoyObzor.xcodeproj', '-scheme', 'DvoynoyObzor',
        '-configuration', 'Release', '-jobs', '2', '-derivedDataPath', derived, 'ARCHS=arm64',
        'ONLY_ACTIVE_ARCH=NO', 'CODE_SIGNING_ALLOWED=NO',
        'PRODUCT_BUNDLE_IDENTIFIER=' + package.BUNDLE_ID, 'build')
    app = stage / 'OpenRadioInspector RC.app'
    run('ditto', derived / 'Build/Products/Release/DvoynoyObzor.app', app)
    (app / 'Contents/MacOS/DvoynoyObzor').rename(app / ('Contents/MacOS/' + package.UI_EXECUTABLE))
    helpers = app / 'Contents/Helpers'; helpers.mkdir(exist_ok=True)
    components = {}
    for name in ['ori-core', 'ori-supervisor']:
        path = helpers / name
        shutil.copy2(output / 'cargo/aarch64-apple-darwin/release' / name, path)
        if run('lipo', '-archs', path, capture=True).strip() != 'arm64': raise ValueError('non-arm64 helper')
        run('codesign', '--force', '--sign', '-', '--timestamp=none', path)
        components[name] = json.loads(run(path, '--build-info', capture=True))
    ui = app / ('Contents/MacOS/' + package.UI_EXECUTABLE)
    if run('lipo', '-archs', ui, capture=True).strip() != 'arm64': raise ValueError('non-arm64 UI')
    for path in [ui, helpers / 'ori-core', helpers / 'ori-supervisor']:
        linked = run('otool', '-L', path, capture=True)
        for line in linked.splitlines()[1:]:
            library = line.strip().split(' (', 1)[0]
            if not library.startswith(('/usr/lib/', '/System/Library/', '@rpath/', '@executable_path/', '@loader_path/')):
                raise ValueError('external development dependency: ' + library)
    agents = app / 'Contents/Library/LaunchAgents'; agents.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'packaging/macos' / (package.AGENT_LABEL + '.plist'), agents / (package.AGENT_LABEL + '.plist'))
    notices(app / 'Contents/Resources/Notices', metadata, Path(run('rustc', '--print', 'sysroot', env=env, capture=True).strip()))
    package.prepare(app, source, components['ori-core'], components['ori-supervisor'],
                    dict(rust=rust, swift=swift, xcode=xcode))
    run('codesign', '--force', '--sign', '-', '--timestamp=none', app)
    run('codesign', '--verify', '--strict', '--deep', '--verbose=2', app)
    manifest = stage / 'manifest.json'; package.evidence(app, manifest)
    shutil.copyfile(ROOT / 'packaging/macos/PERSONAL-RC.txt', stage / 'READ-ME.txt')
    (stage / 'Applications').symlink_to('/Applications', target_is_directory=True)
    stem = 'OpenRadioInspector-' + package.RELEASE_VERSION + '-macos-arm64-adhoc'
    run('hdiutil', 'create', '-volname', 'OpenRadioInspector RC', '-srcfolder', stage,
        '-format', 'UDZO', packages_dir / (stem + '.dmg'))
    run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, packages_dir / (stem + '.app.zip'))
    shutil.copyfile(manifest, packages_dir / (stem + '.manifest.json'))
    shutil.copyfile(stage / 'READ-ME.txt', packages_dir / 'READ-ME.txt')
    checksums = [package.digest(p) + '  ' + p.name for p in sorted(packages_dir.iterdir()) if p.is_file()]
    (packages_dir / 'SHA256SUMS').write_text('\n'.join(checksums) + '\n')
    print(json.dumps(dict(source=source, version=package.RELEASE_VERSION, artifacts=str(packages_dir),
                          hardware_validation='not_run', performance_acceptance='not_run')))


if __name__ == '__main__': main()
