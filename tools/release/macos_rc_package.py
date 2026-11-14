#!/usr/bin/env python3
"""Personal native/Rust RC evidence. Build tool only, never a bundled runtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat

RELEASE_VERSION = '0.4.0-macos.rc.1'
BUNDLE_ID = 'org.openradioinspector.macos.rc'
AGENT_LABEL = BUNDLE_ID + '.core'
UI_EXECUTABLE = 'OpenRadioInspectorRC'
BUILD_INFO = 'Contents/Resources/rc-build-info.json'
PROFILES = {
    'rc1': dict(version=RELEASE_VERSION, build='1', audio=False),
    'rc2-audio': dict(version='0.4.0-macos.rc.2', build='2', audio=True),
}


def release_profile(name):
    try:
        return PROFILES[name]
    except (KeyError, TypeError) as error:
        raise ValueError('unknown personal RC profile') from error


def profile_for_version(version):
    for name, settings in PROFILES.items():
        if settings['version'] == version: return name
    raise ValueError('unknown personal RC release')


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate package metadata key')
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError('nonfinite metadata')


def read_json(path: Path, maximum=1024 * 1024):
    if path.is_symlink() or path.stat().st_size > maximum:
        raise ValueError('unsafe or oversized package metadata')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_object,
                      parse_constant=_nonfinite)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def inventory(app: Path):
    root = app.resolve(strict=True)
    entries = []
    for path in sorted(app.rglob('*')):
        if len(entries) >= 100_000:
            raise ValueError('application inventory exceeds bound')
        relative = path.relative_to(app).as_posix()
        metadata = path.lstat()
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError('broken or cyclic application symlink') from error
            if not target.is_relative_to(root):
                raise ValueError('application symlink escapes its bundle')
            entries.append(dict(path=relative, type='symlink', target=os.readlink(path)))
        elif path.is_file():
            entries.append(dict(path=relative, type='file', size=metadata.st_size,
                                mode=stat.S_IMODE(metadata.st_mode), sha256=digest(path)))
        elif not path.is_dir():
            raise ValueError('unsupported application file type')
    return entries


def verify_inventory(app: Path, expected):
    if inventory(app) != expected:
        raise ValueError('application contents differ from the signed-package inventory')


def validate_runtime_profile(app: Path):
    for path in app.rglob('*'):
        name = path.name.lower()
        if name in {'python.framework', 'python', 'python3', 'node_modules', 'homebrew'} \
                or name.startswith(('libpython', 'libportaudio')) \
                or path.suffix.lower() in {'.py', '.pyc', '.pyd'}:
            raise ValueError('excluded Python/PortAudio/development runtime in personal RC')
    for name in [f'MacOS/{UI_EXECUTABLE}', 'Helpers/ori-core', 'Helpers/ori-supervisor']:
        path = app / 'Contents' / name
        if not path.is_file() or path.is_symlink() or not path.stat().st_mode & stat.S_IXUSR:
            raise ValueError('missing native executable: ' + name)


def validate_components(core, supervisor, source, *, profile='rc1'):
    selected = release_profile(profile)
    if not re.fullmatch('[0-9a-f]{40}', source):
        raise ValueError('an immutable source commit is required')
    for value, name in [(core, 'ori-core'), (supervisor, 'ori-supervisor')]:
        if value.get('component') != name or value.get('source_revision') != source \
                or value.get('target_arch') != 'aarch64' or value.get('target_os') != 'macos' or not value.get('version'):
            raise ValueError('mixed component source, architecture or version')
    if core.get('features') != {'serial': True, 'audio': selected['audio']} or not core.get('sqlite_version'):
        raise ValueError('RC core features differ from the explicitly selected package profile')


def validate_ui_profile(app, *, profile):
    selected = release_profile(profile)
    path = app / 'Contents/Info.plist'
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 65536:
        raise ValueError('invalid RC Info.plist')
    info = plistlib.loads(path.read_bytes())
    if info.get('CFBundleIdentifier') != BUNDLE_ID or info.get('CFBundleExecutable') != UI_EXECUTABLE \
            or info.get('ORIReleaseVersion') != selected['version'] or info.get('ORIPersonalRC') is not True:
        raise ValueError('UI identity differs from the admitted RC profile')
    audio = info.get('ORINativeAudioCapture', False)
    if type(audio) is not bool or audio != selected['audio']:
        raise ValueError('UI audio flag differs from the admitted RC profile')
    purpose = info.get('NSMicrophoneUsageDescription')
    if selected['audio']:
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose.encode('utf-8')) > 4096:
            raise ValueError('audio candidate requires a bounded microphone purpose string')
    elif purpose is not None:
        raise ValueError('RC1 must not declare microphone capture')


def prepare(app: Path, source: str, core, supervisor, toolchains, *, profile='rc1'):
    selected = release_profile(profile)
    validate_components(core, supervisor, source, profile=profile)
    validate_runtime_profile(app)
    info_path = app / 'Contents/Info.plist'
    info = plistlib.loads(info_path.read_bytes())
    info.update(CFBundleIdentifier=BUNDLE_ID, CFBundleExecutable=UI_EXECUTABLE,
                CFBundleName='OpenRadioInspector RC', CFBundleDisplayName='OpenRadioInspector RC',
                CFBundleShortVersionString='0.4.0', CFBundleVersion=selected['build'],
                ORIReleaseVersion=selected['version'], ORIPersonalRC=True,
                ORINativeAudioCapture=selected['audio'])
    if selected['audio']:
        info['NSMicrophoneUsageDescription'] = (
            'Запись выбранного оператором аналогового выхода AR-DV1 через аудиовход Mac. '
            'Фоновая служба продолжает разрешённую запись после закрытия интерфейса.')
    else:
        info.pop('NSMicrophoneUsageDescription', None)
    info['NSAppTransportSecurity'] = {'NSAllowsLocalNetworking': True}
    info_path.write_bytes(plistlib.dumps(info, sort_keys=False))
    components = {}
    for name, value in [('ori-core', core), ('ori-supervisor', supervisor)]:
        relative = 'Contents/Helpers/' + name
        components[name] = dict(path=relative, sha256=digest(app / relative), **value)
    build = dict(schema_version=1, release_version=selected['version'], package_profile=profile,
                 bundle_identifier=BUNDLE_ID,
                 source_revision=source, architecture='arm64', minimum_os='14.0', target_os='27.3',
                 built_at=datetime.now(timezone.utc).isoformat(), components=components,
                 toolchains=toolchains, ui=dict(path='Contents/MacOS/' + UI_EXECUTABLE,
                 integrity='outer code signature; final hash in external manifest'),
                 excluded_components=['Python runtime', 'DuckDB worker'] +
                    ([] if selected['audio'] else ['native audio capture']),
                 audio_validation='not_run' if selected['audio'] else 'unavailable',
                 hardware_validation='not_run', performance_acceptance='not_run')
    write_json(app / BUILD_INFO, build)
    return build


def evidence(app: Path, output: Path):
    validate_runtime_profile(app)
    build = read_json(app / BUILD_INFO, 65_536)
    profile = profile_for_version(build['release_version'])
    if build.get('package_profile', profile) != profile:
        raise ValueError('mixed personal RC package profile')
    validate_components(build['components']['ori-core'], build['components']['ori-supervisor'],
                        build['source_revision'], profile=profile)
    validate_ui_profile(app, profile=profile)
    value = dict(schema_version=1, release_version=build['release_version'],
                 source_revision=build['source_revision'], build_info=build,
                 signature='ad-hoc; Developer ID and notarization not claimed', files=inventory(app))
    # This must stay outside the bundle: signing the outer executable changes
    # its hash. An internal inventory of that executable would be circular.
    if output.resolve().is_relative_to(app.resolve()):
        raise ValueError('final inventory must be outside the signed bundle')
    write_json(output, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    p = sub.add_parser('version'); p.add_argument('--profile', choices=PROFILES, default='rc1')
    p = sub.add_parser('prepare'); p.add_argument('--app', type=Path, required=True)
    p.add_argument('--source', required=True); p.add_argument('--core-info', type=Path, required=True)
    p.add_argument('--supervisor-info', type=Path, required=True); p.add_argument('--toolchains', type=Path, required=True)
    p.add_argument('--profile', choices=PROFILES, default='rc1')
    p = sub.add_parser('evidence'); p.add_argument('--app', type=Path, required=True); p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('verify'); p.add_argument('--app', type=Path, required=True); p.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    if args.operation == 'version': print(release_profile(args.profile)['version'])
    elif args.operation == 'prepare': prepare(args.app, args.source, read_json(args.core_info), read_json(args.supervisor_info), read_json(args.toolchains), profile=args.profile)
    elif args.operation == 'evidence': evidence(args.app, args.output)
    elif args.operation == 'verify':
        validate_runtime_profile(args.app); verify_inventory(args.app, read_json(args.manifest, 32 * 1024 * 1024)['files'])


if __name__ == '__main__':
    main()
