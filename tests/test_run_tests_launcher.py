"""Shell launcher keeps explicit environments and provisions uv dependencies."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == 'posix', 'POSIX shell launcher')
class TestShellLauncher(unittest.TestCase):
    def root(self):
        temporary = tempfile.TemporaryDirectory(prefix='ori launcher ')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        shutil.copy2(Path(__file__).resolve().parents[1] / 'run_tests.sh', root / 'run_tests.sh')
        return root

    def executable(self, path, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n' + source, encoding='utf-8')
        path.chmod(0o700)

    def test_active_environment_takes_precedence_over_local_venv(self):
        root = self.root()
        self.executable(root / '.venv/bin/python', "printf 'local\\n'")
        self.executable(root / 'active/bin/python', "printf 'active\\n'; printf '%s\\n' \"$@\"")
        environment = dict(os.environ, VIRTUAL_ENV=str(root / 'active'))
        result = subprocess.run([shutil.which('bash'), str(root / 'run_tests.sh'), '-v'],
                                env=environment, text=True, capture_output=True, check=True)
        self.assertTrue(result.stdout.startswith('active\n-m\nunittest\n'))
        self.assertTrue(result.stdout.endswith('-v\n'))

    def test_uv_installs_local_project_with_serial_and_test_extras(self):
        root = self.root()
        self.executable(root / 'bin/uv', "printf '%s\\n' \"$@\"")
        environment = dict(os.environ, VIRTUAL_ENV='', PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'])
        result = subprocess.run([shutil.which('bash'), str(root / 'run_tests.sh')],
                                env=environment, text=True, capture_output=True, check=True)
        arguments = result.stdout.splitlines()
        self.assertEqual(arguments[:6], ['run', '--no-project', '--with-requirements',
                                        str(root / 'requirements/core.lock'),
                                        '--with', str(root) + '[serial,test]'])
        self.assertEqual(arguments[6:], ['python', '-m', 'unittest', 'discover', '-s', 'tests', '-t', '.'])
