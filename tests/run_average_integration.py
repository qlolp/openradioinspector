"""Run synthetic average/detection integration checks on a POSIX desktop.

Usage: python -m tests.run_average_integration [--swift]
Requires NumPy, Node and a working Tk desktop; --swift also requires macOS/Xcode.
Owns only its child servers and temporary files, never a user's archive.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--swift', action='store_true', help='Also run both macOS average test suites')
    args = parser.parse_args()
    if os.name != 'posix' or (args.swift and sys.platform != 'darwin'):
        parser.error('Use a POSIX desktop; --swift requires macOS')
    if shutil.which('node') is None:
        parser.error('Node is required to run WebUI checks without skipping them')
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    children = []
    cleanup_errors = []
    with tempfile.TemporaryDirectory(prefix='ori-average-integration-') as directory:
        try:
            for key, module in (
                ('ORI_AVERAGE_SMOKE_BASE_URL', 'tests.average_smoke_server'),
                ('ORI_AVERAGE_DETECTION_SMOKE_BASE_URL', 'tests.sweep_change_detection_smoke_server'),
            ):
                log_path = Path(directory) / (module + '.log')
                log = log_path.open('w')
                try:
                    process = subprocess.Popen(
                        [sys.executable, '-m', module, '--port', '0'], cwd=root,
                        env=environment, stdout=log, stderr=subprocess.STDOUT,
                    )
                except BaseException:
                    log.close()
                    raise
                children.append((module, process, log, log_path))
                deadline = time.monotonic() + 15
                while True:
                    lines = log_path.read_text().splitlines()
                    if lines and 'dump_sha256_before' in lines[0]:
                        metadata = json.loads(lines[0])
                        environment[key] = metadata['url'].removesuffix('/platform')
                        print(f'{module}: {environment[key]}', flush=True)
                        break
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(f'{module} failed to start:\n{log_path.read_text()}')
                    time.sleep(0.05)

            commands = [[sys.executable, '-m', 'unittest',
                         'tests.test_sweep_average', 'tests.test_sweep_average_detection',
                         'tests.test_webui_sweep_average', 'tests.test_windows_sweep_average']]
            if args.swift:
                commands.extend([
                    ['swift', 'test', '--package-path', 'macos/SpektrKomandy', '--filter', 'SweepAverageTests'],
                    ['swift', 'test', '--package-path', 'macos', '--filter', 'SweepAverageModelTests'],
                ])
            for command in commands:
                subprocess.run(command, cwd=root, env=environment, check=True, timeout=300)
        finally:
            # Stop only children created above, even after failed tests or Ctrl-C.
            for module, process, log, log_path in reversed(children):
                try:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    output = log_path.read_text()
                    if process.returncode != 0 or '"unchanged": true' not in output:
                        cleanup_errors.append(f'{module}:\n{output}')
                    else:
                        print(f'{module}: archive unchanged, server stopped', flush=True)
                finally:
                    log.close()
            if cleanup_errors:
                raise RuntimeError('Fixture cleanup/archive verification failed:\n' + '\n'.join(cleanup_errors))


if __name__ == '__main__':
    main()
