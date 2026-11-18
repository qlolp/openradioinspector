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


def configure_test_run(source, profile, *, test_root=None):
    result = copy.deepcopy(source)
    count = 0

    def visit(value):
        nonlocal count
        if isinstance(value, dict):
            if str(value.get('TestBundlePath', '')).endswith('DvoynoyObzorUITests.xctest'):
                value.setdefault('EnvironmentVariables', {})['ORI_UI_TEST_PROFILES'] = str(profile)
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


def passed_cases(output):
    found = re.findall(r"Test Case '-\[[^\]]*WorkbenchUITests (test\w+)\]' passed", output)
    if set(found) != CASES or len(found) != len(CASES) or re.search(r'Test Case .* failed', output):
        raise ValueError('Every named UI case must pass once; missing, failed or retried cases are not success')
    return sorted(found)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--derived-data', type=Path)
    args = parser.parse_args(argv)
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
        run_command(['xcodebuild', '-project', str(ROOT / 'macos/DvoynoyObzor.xcodeproj'),
            '-scheme', 'DvoynoyObzorUI', '-configuration', 'Debug', '-destination',
            'platform=macOS,arch=arm64', '-derivedDataPath', str(derived),
            '-jobs', '2', 'build-for-testing', 'CODE_SIGN_IDENTITY=-'], name='build-for-ui-testing',
            environment=environment, directory=root, timeout=600)
        app = derived / 'Build/Products/Debug/DvoynoyObzor.app'
        executable = app / 'Contents/MacOS/DvoynoyObzor'
        report['native_sha256'] = hashlib.sha256(executable.read_bytes()).hexdigest()
        fixture = Fixture('tests.ui_smoke_server', root, environment)
        endpoint = fixture.ready(20)
        profiles = prepare_profiles(root, endpoint, report['native_sha256'])
        runs = list((derived / 'Build/Products').glob('*.xctestrun'))
        if len(runs) != 1:
            raise ValueError('One generated XCUITest run is required')
        configured = root / 'UI.xctestrun'
        configured.write_bytes(plistlib.dumps(configure_test_run(plistlib.loads(runs[0].read_bytes()),
            profiles, test_root=runs[0].parent)))
        output = run_command(['xcodebuild', 'test-without-building', '-xctestrun', str(configured),
            '-destination', 'platform=macOS,arch=arm64', '-parallel-testing-enabled', 'NO',
            '-resultBundlePath', str(root / 'UI.xcresult')], name='ui-tests',
            environment=environment, directory=root, timeout=600)
        report['test_cases'] = passed_cases(output)
        report['successful'] = True
    except Exception as error:
        report['errors'].append(dict(type=type(error).__name__, message=str(error)))
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
