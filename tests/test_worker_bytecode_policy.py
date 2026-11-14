"""Exercise the trusted Rust argv with an actual isolated private Python prefix."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
import venv

ROOT = Path(__file__).resolve().parents[1]
CONSTRUCTORS = (
    ('crates/ori-core/src/main.rs', 'radioinspector.research_rpc'),
    ('crates/ori-core/examples/benchmark_background.rs', 'radioinspector.research_rpc'),
    ('crates/ori-core/examples/parity_node.rs', 'radioinspector.research_rpc'),
    ('crates/ori-core/src/legacy/worker.rs', 'radioinspector.legacy_rpc'),
    ('crates/ori-core/src/history_jobs.rs', 'ori_research'),
)


def canonical_vector(filename, module):
    text = (ROOT / filename).read_text(encoding='utf-8')
    vectors = []
    for body in re.findall(r'vec!\[([^\]]*)\]', text, flags=re.DOTALL):
        values = re.findall(r'"([^"\\]*)"\.into\(\)', body)
        if values and values[-1] == module:
            vectors.append(values)
    if len(vectors) != 1:
        raise AssertionError(f'{filename}: expected one canonical literal worker argv')
    return vectors[0]


def prefix_inventory(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            value = path.lstat()
            item = {'mode': stat.S_IMODE(value.st_mode)}
            if path.is_symlink():
                item['link'] = os.readlink(path)
            elif path.is_dir():
                item['directory'] = True
            else:
                item['SHA256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            result[path.relative_to(root).as_posix()] = item
    return result


class WorkerBytecodePolicyTests(unittest.TestCase):
    def test_actual_canonical_workers_do_not_write_private_installed_prefix(self):
        # -I deliberately ignores PYTHONDONTWRITEBYTECODE. Only the command-line
        # policy can prevent these writes while retaining isolation. This venv
        # has no pip or scientific packages and never touches the installed315.
        with tempfile.TemporaryDirectory(prefix='ori-worker-bytecode-') as directory:
            temporary = Path(directory)
            prefix = temporary / 'prefix'
            venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(prefix)
            python = prefix / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            site = (prefix / 'Lib/site-packages' if os.name == 'nt' else
                    prefix / f'lib/python{os.sys.version_info.major}.{os.sys.version_info.minor}/site-packages')
            package = site / 'radioinspector'
            package.mkdir()
            (package / '__init__.py').write_text('VALUE = 7\n', encoding='utf-8')
            module_code = ('import sys\nfrom radioinspector import VALUE\n'
                           'print("{\\"result\\":" + str(VALUE) + ",\\"isolated\\":" + str(sys.flags.isolated) + "}")\n')
            for name in ('research_rpc', 'legacy_rpc'):
                (package / (name + '.py')).write_text(module_code, encoding='utf-8')
            research = site / 'ori_research'
            research.mkdir()
            (research / '__init__.py').write_text('', encoding='utf-8')
            (research / '__main__.py').write_text(module_code, encoding='utf-8')
            outside = temporary / 'untrusted-PYTHONPATH'
            outside.mkdir()
            (outside / 'radioinspector.py').write_text('raise RuntimeError("not isolated")\n', encoding='utf-8')
            for index, (filename, module) in enumerate(CONSTRUCTORS):
                with self.subTest(constructor=filename):
                    current_prefix = temporary / f'isolated-{index}'
                    shutil.copytree(prefix, current_prefix, symlinks=True)
                    current_python = current_prefix / python.relative_to(prefix)
                    before = prefix_inventory(current_prefix)
                    arguments = canonical_vector(filename, module)
                    completed = subprocess.run(
                        [str(current_python), *arguments], cwd=temporary,
                        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(outside)),
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=5, check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors='replace'))
                    self.assertEqual(json.loads(completed.stdout), {'result': 7, 'isolated': 1})
                    after = prefix_inventory(current_prefix)
                    self.assertEqual(after, before, f'worker wrote installed cache: {sorted(set(after)-set(before))}')

    def test_exact_allowlists_match_the_canonical_isolated_argv(self):
        private = (ROOT / 'crates/ori-core/src/private_worker.rs').read_text(encoding='utf-8')
        accepted = {tuple(re.findall(r'"([^"\\]*)"', body))
                    for body in re.findall(r'spec\.args\s*==\s*\[([^\]]*)\]', private)}
        expected = {
            tuple(canonical_vector('crates/ori-core/src/main.rs', 'radioinspector.research_rpc')),
            tuple(canonical_vector('crates/ori-core/src/legacy/worker.rs', 'radioinspector.legacy_rpc')),
        }
        self.assertEqual(accepted, expected)
        self.assertEqual({value[-1] for value in accepted}, {'radioinspector.research_rpc', 'radioinspector.legacy_rpc'})
        history = (ROOT / 'crates/ori-history/src/service.rs').read_text(encoding='utf-8')
        body = re.search(r'worker\.args\s*!=\s*\[([^\]]*)\]', history).group(1)
        self.assertEqual(re.findall(r'"([^"\\]*)"', body),
                         canonical_vector('crates/ori-core/src/history_jobs.rs', 'ori_research'))


if __name__ == '__main__':
    unittest.main()
