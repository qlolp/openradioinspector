#!/usr/bin/env python3
"""Build an offline Debian package for Raspberry Pi arm64 or Debian amd64."""
import argparse
from dataclasses import dataclass
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
from tools.release.operator_manual import stage_manual
from tools.release.macos_rc_package import digest

RELEASE = '0.4.0-rc.1'
TEMPLATES = ROOT / 'packaging/debian/templates'
DEPENDENCIES = 'libc6 (>= 2.36), libgcc-s1, ca-certificates, systemd, init-system-helpers'
SYSTEM_LIBRARIES = {'libc.so.6', 'libgcc_s.so.1', 'libm.so.6',
                    'libpthread.so.0', 'libdl.so.2', 'librt.so.1'}


@dataclass(frozen=True)
class DebianPlatform:
    arch: str
    rust_arch: str
    elf_machine: str
    interpreter: str
    package: str
    user: str
    display_name: str
    backend: str
    supported_os: str
    guide: str

    @property
    def target(self):
        return f'{self.rust_arch}-unknown-linux-gnu'

    @property
    def lib(self):
        return Path('usr/lib') / self.package

    @property
    def doc(self):
        return Path('usr/share/doc') / self.package

    @property
    def config(self):
        return Path('etc') / self.package / 'supervisor.json'


PI = DebianPlatform(
    'arm64', 'aarch64', 'AArch64', '/lib/ld-linux-aarch64.so.1',
    'openradioinspector-pi-rc', 'ori-pi-rc', 'OpenRadioInspector Pi RC', 'pi-rc-serial',
    '64-bit Raspberry Pi OS (Debian 12 or later)', 'docs/pi-rc1.md')
AMD64 = DebianPlatform(
    'amd64', 'x86_64', 'Advanced Micro Devices X86-64', '/lib64/ld-linux-x86-64.so.2',
    'openradioinspector-linux', 'ori-linux', 'OpenRadioInspector Linux', 'linux-serial',
    'Debian 12 and 13 (amd64)', 'docs/debian-amd64.md')
PLATFORMS = {'pi-arm64': PI, 'linux-amd64': AMD64}


def capture(*args, env=None):
    return subprocess.check_output(args, cwd=ROOT, text=True, env=env).strip()


def revision(root=ROOT):
    subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=root, check=True)
    # Vite copies public/ wholesale and reads .env.* even when Git ignores them.
    # Check the entire source tree with explicit exceptions for generated outputs.
    inputs = ['.cargo', '.npmrc', 'Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'LICENSE',
              'crates', 'contracts', 'radioinspector/web/static', 'radioinspector/monitoring/storage_window.sql',
              'clients/web', 'packaging/debian/templates', 'packaging/pi/build_rc.py', PI.guide, AMD64.guide,
              'tools/build_modern_web.py', 'tools/release']
    inputs.extend(':(top,exclude)clients/web/' + name for name in
                  ['node_modules', 'dist', '.vite', 'test-results', 'playwright-report'])
    inputs.append(':(top,exclude,glob)tools/release/**/__pycache__/**')
    untracked = subprocess.check_output(['git', 'ls-files', '-z', '--others', '--', *inputs], cwd=root)
    if untracked:
        raise ValueError('Untracked build input: ' + os.fsdecode(untracked.split(b'\0', 1)[0]))
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()


def inspect_elf(path, profile=PI):
    report = capture('readelf', '-WhldV', path, env=dict(os.environ, LC_ALL='C'))
    libraries = sorted(re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', report))
    interpreters = re.findall(r'\[Requesting program interpreter: ([^\]\n]+)\]', report)
    symbols = re.findall(r'Name: GLIBC_(\S+)', report)
    versions = [tuple(map(int, symbol.split('.'))) for symbol in symbols if re.fullmatch(r'\d+(?:\.\d+)+', symbol)]
    # The non-numeric RELR ABI marker was introduced in glibc 2.36.
    versions.extend((2, 36) for symbol in symbols if symbol == 'ABI_DT_RELR')
    allowed = SYSTEM_LIBRARIES | {Path(profile.interpreter).name}
    if (not re.search(r'Class:\s+ELF64\b', report)
            or not re.search(r'Machine:[ \t]+' + re.escape(profile.elf_machine) + r'[ \t]*$', report, re.M)
            or not re.search(r"Data:[ \t]+2's complement, little endian[ \t]*$", report, re.M)
            or interpreters != [profile.interpreter]
            or '(RPATH)' in report or '(RUNPATH)' in report or 'libc.so.6' not in libraries
            or set(libraries) - allowed or len(symbols) != len(versions)
            or not versions or max(versions) > (2, 36)):
        raise ValueError(f'{path.name} must target {profile.arch} Debian 12 system libraries and glibc <= 2.36')
    return {'libraries': libraries, 'highest_required_glibc_symbol': '.'.join(map(str, max(versions)))}


def inventory_components(binaries, source, profile=PI):
    info = {}
    runner = shlex.split(os.environ.get('CARGO_TARGET_' + profile.target.upper().replace('-', '_') + '_RUNNER', ''))
    for name in ['ori-core', 'ori-supervisor']:
        path = binaries / name
        abi = inspect_elf(path, profile)
        component = json.loads(capture(*runner, path, '--build-info'))
        if (component.get('component') != name or component.get('target_arch') != profile.rust_arch
                or component.get('target_os') != 'linux' or component.get('source_revision') != source
                or not component.get('version')):
            raise ValueError(f'Wrong revision, architecture or component identity for {name}')
        if name == 'ori-core' and (component.get('features') != {'serial': True, 'audio': False}
                                   or component.get('sqlite_version') != '3.54.1'):
            raise ValueError('This RC requires serial support and bundled SQLite 3.54.1')
        info[name] = {**component, **abi, 'sha256': digest(path)}
    # These are the exact commands used on first boot and for an older reader.
    with tempfile.TemporaryDirectory(prefix='ori-deb-format-') as temporary:
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


def install_template(name, path, profile, executable=False, version=None):
    fields = dict(PACKAGE=profile.package, SERVICE_USER=profile.user,
                  DISPLAY_NAME=profile.display_name, BACKEND_NAME=profile.backend, VERSION=version)
    template = (TEMPLATES / name).read_text(encoding='ascii')
    content = re.sub(r'@([A-Z_]+)@', lambda match: fields[match[1]], template)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='ascii')
    path.chmod(0o755 if executable else 0o644)


def stage_service(stage, profile):
    for name in ['prepare-archive', 'wait-ready', 'check-install']:
        install_template(name, stage / profile.lib / name, profile, executable=True)
    install_template('supervisor.json', stage / profile.config, profile)
    unit = stage / 'usr/lib/systemd/system' / f'{profile.package}.service'
    install_template('monitoring.service', unit, profile)
    users = stage / 'usr/lib/sysusers.d'
    users.mkdir(parents=True)
    (users / f'{profile.package}.conf').write_text(
        f'u {profile.user} - "{profile.display_name}" /var/lib/{profile.package} /usr/sbin/nologin\n', encoding='ascii')


def assemble(stage, output, debian_revision, env, build, profile=PI):
    version = build['release_version'].replace('-rc.', '~rc.') + f'-{debian_revision}'
    evidence = dict(build, debian_version=version)
    manifest = stage / profile.doc / 'release.json'
    native_notices.write_json(manifest, evidence)
    control = stage / 'DEBIAN'
    control.mkdir(exist_ok=True)
    for script in ['postinst', 'prerm', 'postrm']:
        install_template(script, control / script, profile, executable=True, version=version)
    (control / 'conffiles').write_text(f'/{profile.config.as_posix()}\n', encoding='ascii')
    payload = sorted(path for path in stage.rglob('*') if path.is_file() and control not in path.parents)
    size = sum(path.stat().st_size for path in payload)
    (control / 'control').write_text(
        f'Package: {profile.package}\nVersion: {version}\nArchitecture: {profile.arch}\nSection: science\nPriority: optional\n'
        f'Maintainer: OpenRadioInspector contributors <noreply@users.noreply.github.com>\n'
        f'Depends: {DEPENDENCIES}\nInstalled-Size: {(size + 1023) // 1024}\n'
        f'Description: {profile.display_name} offline monitoring station\n'
        f' Supervised serial measurement core and embedded WebUI for {profile.supported_os}.\n', encoding='ascii')
    # dpkg --verify uses MD5. Distribution and executable integrity use SHA-256.
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
        deb = products / f'{profile.package}_{version}_{profile.arch}.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '--threads-max=2', '-Zxz', '-z4',
                        '--build', stage, deb], env=env, check=True)
        shutil.copyfile(manifest, products / 'release.json')
        shutil.copyfile(stage / profile.doc / 'README.md', products / 'README.md')
        (products / 'SHA256SUMS').write_text(
            ''.join(digest(path) + '  ' + path.name + '\n' for path in sorted(products.iterdir())), encoding='ascii')
        if output.exists():
            raise FileExistsError(output)
        products.rename(output)


def build_rc(output, rehearsal=None, *, distribution=False, profile=PI):
    if platform.system() != 'Linux':
        raise ValueError('Building a Debian candidate requires Linux and dpkg-deb')
    destinations = [output] + ([rehearsal] if rehearsal else [])
    for directory in destinations:
        if directory.exists() or directory.is_symlink() or destinations.count(directory) != 1:
            raise ValueError('Each release output must be a distinct new directory')
        directory.parent.mkdir(exist_ok=True, parents=True)
    source = revision()
    env = dict(os.environ, ORI_BUILD_SOURCE_SHA=source, LIBSQLITE3_SYS_USE_PKG_CONFIG='0',
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
    metadata = native_notices.cargo_metadata(ROOT / 'Cargo.toml', profile.target, ['--no-default-features', '--features', 'ori-core/serial'])
    subprocess.run(['cargo', 'build', '--locked', '--release', '--target', profile.target, '-p', 'ori-core', '-p', 'ori-runtime',
                    '--bins', '--no-default-features', '--features', 'ori-core/serial'], cwd=ROOT, env=env, check=True)
    binaries = Path(metadata['target_directory']) / profile.target / 'release'
    components = inventory_components(binaries, source, profile)
    with tempfile.TemporaryDirectory(prefix='ori-deb-') as temporary:
        stage = Path(temporary)
        for name in components:
            install_file(binaries / name, stage / profile.lib / name, executable=True)
        stage_service(stage, profile)
        doc = stage / profile.doc
        install_file(ROOT / 'LICENSE', doc / 'copyright')
        install_file(ROOT / profile.guide, doc / 'README.md')
        (stage / profile.lib / 'components.sha256').write_text(''.join(
            f"{info['sha256']}  {name}\n" for name, info in sorted(components.items())), encoding='ascii')
        manual = stage_manual(ROOT, doc / 'operator-guide')
        native_notices.collect(doc / 'notices', profile.target, metadata=metadata)
        if revision() != source:
            raise ValueError('Build source changed during packaging')
        evidence = dict(schema_version=1, release_version='0.4.0' if distribution else RELEASE, source_revision=source,
                        target=profile.target, package=profile.package, debian_architecture=profile.arch, manual_sha256=manual,
                        supported_os=profile.supported_os, components=components,
                        cargo_lock_sha256=digest(ROOT / 'Cargo.lock'), npm_lock_sha256=digest(ROOT / 'clients/web/package-lock.json'),
                        license_manifest_sha256=digest(doc / 'notices/licenses.json'), toolchains={'rust': rust, 'node': node},
                        web_assets={f'/modern/{name}': digest(web / name) for name in ui},
                        hardware_validation='not_run')
        if profile == PI:
            evidence['pi_performance'] = 'not_run'
        for index, directory in enumerate(destinations, 1):
            if distribution and directory == rehearsal:
                # The lifecycle VM installs this private RC before the final DEB.
                assemble(stage, directory, 1, env, dict(evidence, release_version=RELEASE), profile)
            else:
                assemble(stage, directory, index, env, evidence, profile)
        print(f'{source}  {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=PLATFORMS, default='pi-arm64')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--rehearsal-dir', type=Path, help='Also build a private upgrade fixture (the preceding RC when freezing 0.4.0)')
    parser.add_argument('--distribution', action='store_true', help='Freeze an internally versioned 0.4.0 DEB')
    args = parser.parse_args()
    build_rc(args.output_dir.absolute(), args.rehearsal_dir.absolute() if args.rehearsal_dir else None,
             distribution=args.distribution, profile=PLATFORMS[args.platform])
