"""Owned XCUITest smoke against a read-only synthetic fixture, never physical acceptance."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import struct
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES = {
    'testNavigationPreservesSessionAndCoreConnection',
    'testPreparationRejectsDuplicatePortsAndAmbiguousFrequency',
    'testReplayFixtureCannotBePresentedAsLiveHardwarePreparation',
    'testArchiveOpensOriginalSpectrumAndMarksHistory',
    'testWorkspaceSwitchKeepsCoreAndSelectedSession',
    'testEventOpensExactOriginalAlongsideReception',
    'testHistoricalEventOpensReadOnlyOriginalWithoutSwitchingAcquisition',
    'testTrainingWindowCaptures',
}


def prepare_profiles(root, endpoint, native_sha256):
    """The owning process writes; the sandboxed XCTest runner only reads."""
    root = Path(root).resolve()
    cases = root / 'cases'
    cases.mkdir(mode=0o700)
    profiles = {}
    for name in sorted(CASES):
        directory = cases / name
        directory.mkdir(mode=0o700)
        evidence = directory / 'native'
        evidence.mkdir(mode=0o700)
        profile = directory / 'profile.json'
        profile.write_text(json.dumps(dict(version=1, run_id='ui-'+uuid.uuid4().hex,
            phase='ui-test', output_dir=str(evidence), expected_ui_sha256=native_sha256,
            core_url=endpoint, session_id='ui-one')) + '\n')
        profile.chmod(0o600)
        profiles[name] = str(profile)
    index = root / 'profiles.json'
    index.write_text(json.dumps(profiles) + '\n')
    index.chmod(0o600)
    return index


def configure_test_run(source, profile, *, test_root=None, target_app=None):
    result = copy.deepcopy(source)
    count = 0

    def visit(value):
        nonlocal count
        if isinstance(value, dict):
            if str(value.get('TestBundlePath', '')).endswith('DvoynoyObzorUITests.xctest'):
                value.setdefault('EnvironmentVariables', {})['ORI_UI_TEST_PROFILES'] = str(profile)
                if target_app is not None:
                    value['EnvironmentVariables']['ORI_UI_TEST_APP'] = str(target_app)
                    value['UITargetAppPath'] = str(target_app)
                count += 1
            for key, child in list(value.items()):
                if test_root is not None and isinstance(child, str) and child.startswith('__TESTROOT__/'):
                    value[key] = str(test_root) + child[len('__TESTROOT__'):]
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(result)
    if count != 1:
        raise ValueError('Exactly one generated DvoynoyObzorUITests target is required')
    return result


def passed_cases(output, expected=CASES):
    found = re.findall(r"Test Case '-\[[^\]]*WorkbenchUITests (test\w+)\]' passed", output)
    if set(found) != expected or len(found) != len(expected) or re.search(r'Test Case .* failed', output):
        raise ValueError('Every named UI case must pass once; missing, failed or retried cases are not success')
    return sorted(found)


def confirm_idle_target(app):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    bundle = info['CFBundleIdentifier']
    check = ("ObjC.import('AppKit'); function run(args) { return "
             "Number($.NSRunningApplication.runningApplicationsWithBundleIdentifier(args[0]).count) }")
    if subprocess.check_output(['osascript', '-l', 'JavaScript', '-e', check, bundle], text=True).strip() != '0':
        raise ValueError('The target application is already running. Its active instance was left open.')
    facts = plistlib.loads(subprocess.check_output(['ioreg', '-n', 'Root', '-d1', '-a']))
    if isinstance(facts, list):
        facts = facts[0]
    if facts.get('IOConsoleLocked') is not False or os.stat('/dev/console').st_uid != os.geteuid():
        raise ValueError('The current operator console is locked or unavailable for window capture')
    return info


def publish_training_window(data, filename, destination):
    if data[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR':
        raise ValueError('The training window attachment is not a PNG')
    width, height = struct.unpack('>II', data[16:24])
    if width < 920 or height < 580:
        raise ValueError('Training application window is too small')
    (destination / filename).write_bytes(data)
    return dict(width=width, height=height, sha256=hashlib.sha256(data).hexdigest())


def collect_training_images(directory, destination):
    report = {}
    images = {'macos-monitoring', 'macos-event'}
    manifest = json.loads((directory / 'manifest.json').read_bytes())
    for test in manifest:
        if test['testIdentifier'].split('/')[-1].rstrip('()') != 'testTrainingWindowCaptures':
            continue
        for item in test['attachments']:
            if item['isAssociatedWithFailure']:
                continue
            name = item['suggestedHumanReadableName']
            matches = [label for label in images if label + '.png' in name]
            if len(matches) != 1:
                continue
            label = matches[0]
            if label in report:
                raise ValueError('Duplicate training screenshot')
            path = (directory / item['exportedFileName']).resolve()
            if path.parent != directory.resolve():
                raise ValueError('Invalid attachment path')
            filename = label + '.png'
            report[filename] = publish_training_window(path.read_bytes(), filename, destination)
    if len(report) != len(images):
        raise ValueError('Missing named training window screenshots')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--derived-data', type=Path)
    parser.add_argument('--installed-app', type=Path, help='Exercise an idle app from the installed candidate')
    parser.add_argument('--expected-ui-sha256', help='Executable digest from the candidate manifest')
    parser.add_argument('--guide-only', action='store_true', help='Run just the anonymized window capture case')
    args = parser.parse_args(argv)
    if bool(args.installed_app) != bool(args.expected_ui_sha256):
        parser.error('--installed-app and --expected-ui-sha256 must be provided together')
    if sys.platform != 'darwin':
        parser.error('XCUITest requires the target Mac with Xcode and an unlocked console')
    from tests.run_release_checks import Fixture, run_command
    os.umask(0o077)
    root = args.report_dir.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    derived = args.derived_data.resolve() if args.derived_data else root / 'DerivedData'
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('ORI_RUNTIME_', 'PYTHON', 'DYLD_'))}
    environment.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    environment['PATH'] = str(Path(sys.executable).parent) + os.pathsep + environment.get('PATH', '')
    registry = root / 'fixture-registry'
    registry.mkdir(mode=0o700)
    environment['ORI_RELEASE_PROCESS_REGISTRY'] = str(registry)
    report = dict(profile='macos-ui-automation', successful=False, hardware_tested=False,
                  point2_passed=False, point4_passed=False, errors=[], cleanup_errors=[])
    fixture = None
    try:
        if not (args.guide_only and args.installed_app):
            run_command(['xcodebuild', '-project', str(ROOT / 'macos/DvoynoyObzor.xcodeproj'),
                '-scheme', 'DvoynoyObzorUI', '-configuration', 'Debug', '-destination',
                'platform=macOS,arch=arm64', '-derivedDataPath', str(derived),
                '-jobs', '2', 'build-for-testing', 'CODE_SIGN_IDENTITY=-'], name='build-for-ui-testing',
                environment=environment, directory=root, timeout=600)
        app = args.installed_app.resolve() if args.installed_app else derived / 'Build/Products/Debug/DvoynoyObzor.app'
        info = confirm_idle_target(app)
        executable = app / 'Contents/MacOS' / info['CFBundleExecutable']
        report['target'] = 'installed_bundle' if args.installed_app else 'xcode_debug_build'
        report['fixture'] = 'tests.ui_smoke_server'
        report['source_revision'] = subprocess.check_output(['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip()
        report['source_dirty'] = bool(subprocess.check_output(['git', '-C', ROOT, 'status', '--porcelain']))
        if args.installed_app:
            build = json.loads((app / 'Contents/Resources/rc-build-info.json').read_bytes())
            report['app_source_revision'] = build['source_revision']
            report['app_version'] = build['release_version']
            subprocess.run(['codesign', '--verify', '--strict', '--deep', app], check=True)
            signing = subprocess.run(['codesign', '-dv', '--verbose=2', app], check=True, capture_output=True, text=True)
            report['developer_id'] = 'Authority=Developer ID Application:' in signing.stderr
            if build['source_revision'] != report['source_revision'] or report['source_dirty']:
                raise ValueError('Installed guide captures require a clean checkout of the embedded source revision')
        report['native_sha256'] = hashlib.sha256(executable.read_bytes()).hexdigest()
        if args.expected_ui_sha256 and report['native_sha256'] != args.expected_ui_sha256:
            raise ValueError('Installed executable differs from the candidate digest')
        fixture = Fixture('tests.ui_smoke_server', root, environment)
        endpoint = fixture.ready(20)
        profiles = prepare_profiles(root, endpoint, report['native_sha256'])
        if args.guide_only:
            profile_path = Path(json.loads(profiles.read_text())['testTrainingWindowCaptures'])
            profile = json.loads(profile_path.read_text())
            output = Path(profile['output_dir'])
            (output / 'training-window.request').write_text(profile['run_id'])
            run_command(['swift', str(ROOT / 'macos/ci/launch_training_app.swift'), str(app), str(profile_path)],
                        name='guide-window', directory=root, environment=environment, timeout=120)
            receipt = json.loads((output / 'training-windows.json').read_text())
            if receipt.get('run_id') != profile['run_id'] or not receipt.get('successful'):
                raise ValueError(receipt.get('error', 'Training window capture did not complete'))
            report['capture'] = 'ScreenCaptureKit.currentProcess'
            report['images'] = {}
            for filename in ('macos-monitoring.png', 'macos-event.png'):
                report['images'][filename] = publish_training_window((output / filename).read_bytes(), filename, root)
        else:
            runs = list((derived / 'Build/Products').glob('*.xctestrun'))
            if len(runs) != 1:
                raise ValueError('One generated XCUITest run is required')
            configured = root / 'UI.xctestrun'
            configured.write_bytes(plistlib.dumps(configure_test_run(plistlib.loads(runs[0].read_bytes()),
                profiles, test_root=runs[0].parent, target_app=app if args.installed_app else None)))
            output = run_command(['xcodebuild', 'test-without-building', '-xctestrun', str(configured),
                '-destination', 'platform=macOS,arch=arm64', '-parallel-testing-enabled', 'NO',
                '-resultBundlePath', str(root / 'UI.xcresult')], name='ui-tests',
                environment=environment, directory=root, timeout=600)
            report['test_cases'] = passed_cases(output)
            attachments = root / 'training-attachments'
            run_command(['xcrun', 'xcresulttool', 'export', 'attachments', '--path', str(root / 'UI.xcresult'),
                '--output-path', str(attachments), '--filter', '*macos-*.png*'],
                name='export-training-windows', directory=root, environment=environment, timeout=60)
            report['images'] = collect_training_images(attachments, root)
        report['successful'] = True
    except Exception as error:
        report['errors'].append(dict(type=type(error).__name__, message=str(error)))
        from macos.ci.report_command import report_log_annotations
        report['diagnostic_annotations'] = report_log_annotations(root)
    finally:
        if fixture is not None:
            try:
                fixture.close(15)
                report['fixture_originals_unchanged'] = True
            except Exception as error:
                report['cleanup_errors'].append(str(error))
        report['successful'] &= not report['cleanup_errors']
        (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)
    return 0 if report['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
