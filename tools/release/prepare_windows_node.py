#!/usr/bin/env python3
"""Stage the native x64 RC helpers and one production Web build for Tauri."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / 'clients/windows'
TARGET = 'x86_64-pc-windows-msvc'
VERSION = '0.4.0-rc.1'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def helper_record(path, name, revision, info):
    if (len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision)
            or info.get('schema_version') != 1 or info.get('component') != name
            or info.get('source_revision') != revision or info.get('target_os') != 'windows'
            or info.get('target_arch') != 'x86_64'):
        raise ValueError(f'{name} does not match the Windows RC revision and target')
    if name == 'ori-core' and info.get('features') != {'serial': True, 'audio': False}:
        raise ValueError('Windows RC requires the receive-only serial build')
    return {**info, 'sha256': digest(path)}


def stage_web(source, destination):
    manifest = json.loads((source / 'modern-build.json').read_text(encoding='utf-8'))
    if manifest.get('offline') is not True or 'index.html' not in manifest['files']:
        raise ValueError('missing offline web build')
    destination.mkdir()
    # Tauri loads index.html from its app origin; the shared Vite build resolves
    # every lazy chunk, stylesheet and worker at the same /modern/ path as ori-core.
    shutil.copytree(source, destination / 'modern')
    shutil.copyfile(source / 'index.html', destination / 'index.html')
    return {name: digest(source / name) for name in manifest['files']}


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True, **kwargs)


def main():
    if sys.platform != 'win32':
        raise SystemExit('Build the native NSIS installer on Windows x64.')
    revision = run('git', 'rev-parse', 'HEAD', capture_output=True, text=True).stdout.strip()
    run('git', 'diff', '--quiet', 'HEAD', '--')
    env = {**os.environ, 'ORI_BUILD_SOURCE_SHA': revision}
    npm = shutil.which('npm.cmd')
    if not npm:
        raise SystemExit('npm.cmd unavailable')
    run(npm, '--prefix', 'clients/web', 'run', 'build')
    run(sys.executable, 'tools/build_modern_web.py')
    run(sys.executable, 'tools/build_modern_web.py', '--check')
    run('cargo', 'build', '--release', '--locked', '--target', TARGET, '--no-default-features',
        '--features', 'ori-core/serial', '-p', 'ori-core', '-p', 'ori-runtime', env=env)
    metadata = json.loads(run('cargo', 'metadata', '--locked', '--format-version', '1',
                              '--no-deps', capture_output=True).stdout)
    binaries = Path(metadata['target_directory']) / TARGET / 'release'
    with tempfile.TemporaryDirectory(prefix='.node-build-', dir=WINDOWS) as tmp:
        temp = Path(tmp)
        runtime = temp / 'runtime'
        runtime.mkdir()
        shutil.copyfile(WINDOWS / 'runtime/README.txt', runtime / 'README.txt')
        records = {}
        for name in ('ori-core', 'ori-supervisor'):
            executable = runtime / (name + '.exe')
            shutil.copy2(binaries / executable.name, executable)
            info = json.loads(run(executable, '--build-info', capture_output=True).stdout)
            records[name] = helper_record(executable, name, revision, info)
        web = stage_web(ROOT / 'radioinspector/web/modern', temp / 'frontend')
        shutil.copyfile(ROOT / 'deploy/windows/register-user-task.ps1', runtime / 'register-user-task.ps1')
        run(sys.executable, 'tools/release/native_notices.py', '--target', TARGET,
            '--output', runtime / 'notices', '--tauri')
        manifest = {'schema_version': 1, 'release_version': VERSION, 'target': TARGET,
                    'source_revision': revision, 'components': records, 'web': web}
        (runtime / 'rc-build-info.json').write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        for name in ('runtime', 'frontend'):
            previous = WINDOWS / name
            if previous.exists():
                previous.rename(temp / (name + '.previous'))
            try:
                (temp / name).rename(previous)
            except BaseException:
                if (temp / (name + '.previous')).exists():
                    (temp / (name + '.previous')).rename(previous)
                raise


if __name__ == '__main__':
    main()
