#!/usr/bin/env python3
"""Build the offline Raspberry Pi arm64 Debian candidate from this checkout."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import build_modern_web
from tools.release import native_notices
from tools.release.macos_rc_package import digest

PACKAGE = 'openradioinspector-pi-rc'
RELEASE = '0.4.0-rc.1'
TARGET = 'aarch64-unknown-linux-gnu'
RC = ROOT / 'packaging/pi/rc'
LIB = Path('usr/lib') / PACKAGE
DOC = Path('usr/share/doc') / PACKAGE
CONFIG = Path('etc') / PACKAGE / 'supervisor.json'
DEPENDENCIES = 'libc6 (>= 2.36), libgcc-s1, systemd, init-system-helpers'
SYSTEM_LIBRARIES = {'ld-linux-aarch64.so.1', 'libc.so.6', 'libgcc_s.so.1', 'libm.so.6',
                    'libpthread.so.0', 'libdl.so.2', 'librt.so.1'}


def capture(*args, env=None):
    return subprocess.check_output(args, cwd=ROOT, text=True, env=env).strip()


def revision():
    subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=ROOT, check=True)
    sources = ['crates', 'contracts', 'radioinspector/web/static', 'clients/web/src', 'packaging/pi', 'tools/release']
    if capture('git', 'ls-files', '--others', '--exclude-standard', '--', *sources):
        raise ValueError('Commit untracked build inputs before creating a release')
    return capture('git', 'rev-parse', 'HEAD')


def inspect_elf(path):
    report = capture('readelf', '-WhldV', path, env=dict(os.environ, LC_ALL='C'))
    libraries = sorted(re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', report))
    versions = [tuple(map(int, version.split('.'))) for version in re.findall(r'Name: GLIBC_(\d+(?:\.\d+)+)', report)]
    if (not re.search(r'Class:\s+ELF64\b', report) or not re.search(r'Machine:\s+AArch64\b', report)
            or 'little endian' not in report or '/lib/ld-linux-aarch64.so.1]' not in report
            or '(RPATH)' in report or '(RUNPATH)' in report or not libraries or set(libraries) - SYSTEM_LIBRARIES
            or not versions or max(versions) > (2, 36)):
        raise ValueError(f'{path.name} must target arm64 Debian 12 system libraries and glibc <= 2.36')
    return {'libraries': libraries, 'highest_required_glibc_symbol': '.'.join(map(str, max(versions)))}


def inventory_components(binaries, source):
    info = {}
    runner = shlex.split(os.environ.get('CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUNNER', ''))
    for name in ['ori-core', 'ori-supervisor']:
        path = binaries / name
        abi = inspect_elf(path)
        component = json.loads(capture(*runner, path, '--build-info'))
        if (component.get('component') != name or component.get('target_arch') != 'aarch64'
                or component.get('target_os') != 'linux' or component.get('source_revision') != source
                or not component.get('version')):
            raise ValueError(f'Wrong revision, architecture or component identity for {name}')
        if name == 'ori-core' and (component.get('features') != {'serial': True, 'audio': False}
                                   or not component.get('sqlite_version')):
            raise ValueError('This RC needs real serial support and bundled SQLite')
        info[name] = {**component, **abi, 'sha256': digest(path)}
    # These are the exact commands used on first boot and for an older reader.
    with tempfile.TemporaryDirectory(prefix='ori-pi-format-') as temporary:
        compact = Path(temporary) / 'sessions.sqlite3'
        compatible = Path(temporary) / 'compatible.sqlite'
        core = [*runner, binaries / 'ori-core']
        subprocess.run([*core, 'archive-init-compact', compact], check=True)
        subprocess.run([*core, 'archive-materialize', compact, compatible], check=True)
        for archive, version in [(compact, 7), (compatible, 6)]:
            with sqlite3.connect(archive.as_uri() + '?mode=ro', uri=True) as db:
                if db.execute('PRAGMA user_version').fetchone() != (version,) or db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                    raise ValueError('Packaged archive creation or materialization failed')
    return info


def install_file(source, path, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, path)
    path.chmod(0o755 if executable else 0o644)


def assemble(stage, output, debian_revision, env, build):
    version = f'0.4.0~rc.1-{debian_revision}'
    evidence = dict(build, debian_version=version)
    manifest = stage / DOC / 'release.json'
    native_notices.write_json(manifest, evidence)
    control = stage / 'DEBIAN'
    control.mkdir(exist_ok=True)
    for script in ['postinst', 'prerm', 'postrm']:
        install_file(RC / script, control / script, executable=True)
    (control / 'conffiles').write_text(f'/{CONFIG.as_posix()}\n', encoding='ascii')
    payload = sorted(path for path in stage.rglob('*') if path.is_file() and control not in path.parents)
    size = sum(path.stat().st_size for path in payload)
    (control / 'control').write_text(
        f'Package: {PACKAGE}\nVersion: {version}\nArchitecture: arm64\nSection: science\nPriority: optional\n'
        f'Maintainer: OpenRadioInspector contributors <noreply@users.noreply.github.com>\n'
        f'Depends: {DEPENDENCIES}\nInstalled-Size: {(size + 1023) // 1024}\n'
        'Description: Offline Raspberry Pi monitoring release candidate\n'
        ' Supervised serial measurement core and embedded WebUI for Raspberry Pi OS 64-bit.\n', encoding='ascii')
    # dpkg --verify uses MD5; distribution and executable integrity use SHA-256.
    with (control / 'md5sums').open('w', encoding='utf-8') as sums:
        for path in payload:
            sums.write(hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
                       + '  ' + path.relative_to(stage).as_posix() + '\n')
    for path in [stage, *stage.rglob('*')]:
        if path.is_dir():
            path.chmod(0o755)
        elif not path.stat().st_mode & 0o100:
            path.chmod(0o644)
    # Publish only the completed artifacts. Reusing an existing release directory is an error.
    with tempfile.TemporaryDirectory(prefix='.ori-packages-', dir=output.parent) as temporary:
        products = Path(temporary) / 'packages'
        products.mkdir()
        deb = products / f'{PACKAGE}_{version}_arm64.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '--threads-max=2', '-Zxz', '-z4',
                        '--build', stage, deb], env=env, check=True)
        shutil.copyfile(manifest, products / 'release.json')
        shutil.copyfile(stage / DOC / 'README.md', products / 'README.md')
        (products / 'SHA256SUMS').write_text(
            ''.join(digest(path) + '  ' + path.name + '\n' for path in sorted(products.iterdir())), encoding='ascii')
        if output.exists():
            raise FileExistsError(output)
        products.rename(output)


def build_rc(output, rehearsal=None):
    if platform.system() != 'Linux':
        raise ValueError('Building a Debian candidate requires Linux and dpkg-deb')
    destinations = [output] + ([rehearsal] if rehearsal else [])
    for directory in destinations:
        if directory.exists() or directory.is_symlink() or destinations.count(directory) != 1:
            raise ValueError('Each release output must be a distinct new directory')
        directory.parent.mkdir(exist_ok=True, parents=True)
    source = revision()
    env = dict(os.environ, ORI_BUILD_SOURCE_SHA=source,
               SOURCE_DATE_EPOCH=capture('git', 'show', '-s', '--format=%ct', source))
    rust = capture('rustc', '--version')
    node = capture('node', '--version')
    if not rust.startswith('rustc 1.99.1 ') or node != 'v26.13.0':
        raise ValueError('The release build requires Rust 1.99.1 and Node 26.13.0')
    subprocess.run(['npm', '--prefix', 'clients/web', 'run', 'build'], cwd=ROOT, env=env, check=True)
    web = build_modern_web.package(ROOT / 'clients/web/dist', ROOT / 'radioinspector/web/modern')
    build_modern_web.check(web)
    ui = json.loads((web / 'modern-build.json').read_text(encoding='utf-8'))['files']
    if not any(name.endswith('.js') for name in ui) or not any(name.endswith('.css') for name in ui):
        raise ValueError('Expected the production Vite application and styles')
    metadata = native_notices.cargo_metadata(ROOT / 'Cargo.toml', TARGET, ['--no-default-features', '--features', 'ori-core/serial'])
    subprocess.run(['cargo', 'build', '--locked', '--release', '--target', TARGET, '-p', 'ori-core', '-p', 'ori-runtime',
                    '--bins', '--no-default-features', '--features', 'ori-core/serial'], cwd=ROOT, env=env, check=True)
    binaries = Path(metadata['target_directory']) / TARGET / 'release'
    components = inventory_components(binaries, source)
    with tempfile.TemporaryDirectory(prefix='ori-deb-') as temporary:
        stage = Path(temporary)
        for name in components:
            install_file(binaries / name, stage / LIB / name, executable=True)
        for name in ['prepare-archive', 'wait-ready']:
            install_file(RC / name, stage / LIB / name, executable=True)
        install_file(RC / 'supervisor.json', stage / CONFIG)
        install_file(RC / f'{PACKAGE}.service', stage / 'usr/lib/systemd/system' / f'{PACKAGE}.service')
        users = stage / 'usr/lib/sysusers.d'
        users.mkdir(parents=True)
        (users / f'{PACKAGE}.conf').write_text(
            f'u ori-pi-rc - "OpenRadioInspector Pi RC" /var/lib/{PACKAGE} /usr/sbin/nologin\n', encoding='ascii')
        install_file(ROOT / 'LICENSE', stage / DOC / 'copyright')
        install_file(ROOT / 'docs/pi-rc1.md', stage / DOC / 'README.md')
        native_notices.collect(stage / DOC / 'notices', TARGET, metadata=metadata)
        if revision() != source:
            raise ValueError('Build source changed during packaging')
        evidence = dict(schema_version=1, release_version=RELEASE, source_revision=source, target=TARGET,
                        supported_os='64-bit Raspberry Pi OS (Debian 12 or later)', components=components,
                        cargo_lock_sha256=digest(ROOT / 'Cargo.lock'), npm_lock_sha256=digest(ROOT / 'clients/web/package-lock.json'),
                        license_manifest_sha256=digest(stage / DOC / 'notices/licenses.json'), toolchains={'rust': rust, 'node': node},
                        web_assets={f'/modern/{name}': digest(web / name) for name in ui},
                        hardware_validation='not_run', pi_performance='not_run')
        for index, directory in enumerate(destinations, 1):
            assemble(stage, directory, index, env, evidence)
        print(f'{source}  {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--rehearsal-dir', type=Path, help='Also make a private second Debian revision for lifecycle testing')
    args = parser.parse_args()
    build_rc(args.output_dir.absolute(), args.rehearsal_dir.absolute() if args.rehearsal_dir else None)
