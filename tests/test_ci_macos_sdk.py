"""Portable command-capture tests; no Apple SDK or compiler is executed."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import run_release_checks as release


HELPER = Path(__file__).resolve().parents[1] / 'macos/ci/prepare_c_sdk.py'


class CapturedSDK:
    def __init__(self, root):
        root = root.resolve()
        self.root = root
        self.developer = root / 'Fixture Xcode.app/Contents/Developer'
        self.sdk = self.developer / 'Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'
        self.header = self.sdk / 'usr/include/stdio.h'
        self.header.parent.mkdir(parents=True)
        self.header.write_text('/* fixture only, never compiled */\n')
        tools = self.developer / 'Toolchains/XcodeDefault.xctoolchain/usr/bin'
        tools.mkdir(parents=True)
        self.cc, self.cxx = tools / 'clang', tools / 'clang++'
        for tool in (self.cc, self.cxx):
            tool.write_text('fixture compiler: never executed\n')
            tool.chmod(0o700)
        self.environment_file = root / 'github-env'
        self.environment_file.write_text('PREVIOUS=retained\n')
        self.environment = {
            'DEVELOPER_DIR': str(self.developer), 'RUNNER_TEMP': str(root),
            'GITHUB_ENV': str(self.environment_file), 'HOME': str(root / 'original-home'),
            'TOOLCHAINS': 'fixture-custom-swift', 'SDKROOT': '/stale-sdk', 'CC': '/stale-cc',
        }
        self.commands = []
        self.compile_error = False

    def run(self, argv, **options):
        self.commands.append((list(argv), options))
        answers = {
            ('xcrun', '--sdk', 'macosx', '--show-sdk-path'): str(self.sdk),
            ('xcrun', '--toolchain', 'XcodeDefault', '--sdk', 'macosx', '--find', 'clang'): str(self.cc),
            ('xcrun', '--toolchain', 'XcodeDefault', '--sdk', 'macosx', '--find', 'clang++'): str(self.cxx),
        }
        if tuple(argv) in answers:
            return subprocess.CompletedProcess(argv, 0, answers[tuple(argv)] + '\n', '')
        if argv[0] != str(self.cc):
            raise AssertionError('Unexpected external command')
        self.compile_source = Path(argv[argv.index('-c') + 1]).read_text()
        self.compile_environment = dict(options['env'])
        if self.compile_error:
            raise subprocess.CalledProcessError(1, argv, stderr='fixture: stdio.h unavailable')
        Path(argv[argv.index('-o') + 1]).write_bytes(b'fixture object, not executable')
        return subprocess.CompletedProcess(argv, 0, '', '')


class MacOSSDKSelectionTests(unittest.TestCase):
    def prepare(self, host):
        self.assertTrue(HELPER.is_file(), 'SDK selection must bind the C compiler and SDK before Cargo')
        spec = importlib.util.spec_from_file_location('ci_apple_sdk_capture', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.prepare(host.environment, run=host.run)

    def test_selected_sdk_and_default_apple_clang_reach_later_release_children(self):
        # Catches selecting only Swift/DEVELOPER_DIR or keeping relative "cc".
        with tempfile.TemporaryDirectory() as name:
            host = CapturedSDK(Path(name))
            original = dict(host.environment)
            result = self.prepare(host)
            exports = dict(line.split('=', 1) for line in host.environment_file.read_text().splitlines())
            self.assertEqual(exports, {'PREVIOUS': 'retained', 'SDKROOT': str(host.sdk),
                                      'CC': str(host.cc), 'CXX': str(host.cxx)})
            self.assertEqual(host.environment, original)
            self.assertEqual(result['SDKROOT'], str(host.sdk))
            compile_argv = host.commands[-1][0]
            self.assertEqual(compile_argv[:3], [str(host.cc), '-isysroot', str(host.sdk)])
            self.assertIn('#include <stdio.h>', host.compile_source)
            self.assertIn('#include <stdint.h>', host.compile_source)
            self.assertNotEqual(host.compile_environment['HOME'], original['HOME'])
            self.assertEqual(host.compile_environment['SDKROOT'], str(host.sdk))
            self.assertEqual(host.compile_environment['TOOLCHAINS'], 'fixture-custom-swift')
            with patch.dict(os.environ, exports | {'DEVELOPER_DIR': str(host.developer)}, clear=True):
                child = release.child_environment()
            self.assertEqual({key: child[key] for key in ('SDKROOT', 'CC', 'CXX')},
                             {key: exports[key] for key in ('SDKROOT', 'CC', 'CXX')})

    def test_failed_header_compile_never_publishes_a_ready_sdk(self):
        with tempfile.TemporaryDirectory() as name:
            host = CapturedSDK(Path(name))
            host.compile_error = True
            with self.assertRaises(subprocess.CalledProcessError):
                self.prepare(host)
            self.assertEqual(host.environment_file.read_text(), 'PREVIOUS=retained\n')

    def test_missing_stdio_is_a_setup_failure_before_compilation_or_export(self):
        with tempfile.TemporaryDirectory() as name:
            host = CapturedSDK(Path(name))
            host.header.unlink()
            with self.assertRaisesRegex(ValueError, 'stdio'):
                self.prepare(host)
            self.assertFalse(any(argv[0] == str(host.cc) for argv, _ in host.commands))
            self.assertEqual(host.environment_file.read_text(), 'PREVIOUS=retained\n')

    def test_sdk_from_another_xcode_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as name:
            host = CapturedSDK(Path(name))
            host.sdk = host.root / 'OtherXcode/MacOSX.sdk'
            header = host.sdk / 'usr/include/stdio.h'
            header.parent.mkdir(parents=True)
            header.write_text('/* foreign fixture SDK */\n')
            with self.assertRaisesRegex(ValueError, 'selected Xcode'):
                self.prepare(host)
            self.assertEqual(host.environment_file.read_text(), 'PREVIOUS=retained\n')

    def test_compiler_outside_selected_xcode_is_not_a_fallback(self):
        with tempfile.TemporaryDirectory() as name:
            host = CapturedSDK(Path(name))
            host.cc = host.root / 'foreign-clang'
            host.cc.write_text('not selected\n')
            host.cc.chmod(0o700)
            with self.assertRaisesRegex(ValueError, 'selected Xcode'):
                self.prepare(host)
            self.assertEqual(host.environment_file.read_text(), 'PREVIOUS=retained\n')


if __name__ == '__main__':
    unittest.main()
