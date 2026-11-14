#!/usr/bin/env python3
"""Check a relocated signed RC without registering its service or using USB."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from macos_rc_package import RELEASE_VERSION, UI_EXECUTABLE, read_json, verify_inventory, validate_runtime_profile


def request(url, method, path, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    with urlopen(Request(url + path, data=data, method=method, headers={'Content-Type': 'application/json'}), timeout=10) as response:
        return json.load(response)['data']


@contextmanager
def core(executable, data, env):
    ready = data / 'ready.url'
    with (data / 'core.log').open('ab') as log:
        child = subprocess.Popen([str(executable), '--mode', 'live', '--data-dir', str(data),
            '--port', '0', '--ready-file', str(ready), '--stdin-lifeline'],
            stdin=subprocess.PIPE, stdout=log, stderr=log, env=env, cwd=data)
        try:
            deadline = time.monotonic() + 30
            while not ready.exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('owned packaged core did not become ready; see core.log')
                time.sleep(.05)
            url = ready.read_text().strip()
            if not url.startswith('http://127.0.0.1:'): raise RuntimeError('non-loopback readiness')
            yield url
            child.stdin.close(); child.stdin = None
            child.wait(timeout=120)
            if child.returncode != 0 or ready.exists(): raise RuntimeError('core did not stop cleanly/release readiness')
        finally:
            if child.poll() is None:
                child.stdin.close(); child.stdin = None
                try: child.wait(timeout=120)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    args.app = args.app.resolve(strict=True)
    args.manifest = args.manifest.resolve(strict=True)
    args.report_dir = args.report_dir.resolve()
    args.report_dir.mkdir(parents=True, exist_ok=False)
    outcome = {'release_version': RELEASE_VERSION, 'successful': False,
        'scope': 'relocated packaged-code/inventory, cold archive restore; no SM registration, native rendering or physical acceptance',
        'hardware_validation': 'not_run', 'target_macos_27_3': 'not_run'}
    try:
        validate_runtime_profile(args.app)
        manifest = read_json(args.manifest, 32 * 1024 * 1024)
        verify_inventory(args.app, manifest['files'])
        # No checkout/module environment or package manager is available to the
        # child. Preserve the actual login HOME; data is explicitly temporary.
        env = os.environ.copy(); env['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
        for key in list(env):
            if key.startswith(('PYTHON', 'ORI_RUNTIME_', 'DYLD_', 'CARGO_', 'RUSTUP_')) or key == 'VIRTUAL_ENV': env.pop(key)
        subprocess.run(['codesign', '--verify', '--strict', '--deep', str(args.app)], check=True, env=env)
        for name in ['ori-core', 'ori-supervisor']:
            actual = json.loads(subprocess.check_output([str(args.app/'Contents/Helpers'/name), '--build-info'], env=env, cwd=args.report_dir))
            expected = manifest['build_info']['components'][name]
            for key in actual:
                if actual[key] != expected[key]: raise RuntimeError('packaged component metadata mismatch: ' + name)
        outcome['native_self_check'] = json.loads(subprocess.check_output(
            [str(args.app/'Contents/MacOS'/UI_EXECUTABLE), '--self-check'], env=env, cwd=args.report_dir))
        fixture = read_json(Path(__file__).resolve().parents[2] / 'contracts/fixtures/measurement-v1.json')
        executable = args.app / 'Contents/Helpers/ori-core'
        data = args.report_dir / 'source'; data.mkdir()
        route = '/api/v1/sessions/' + quote(fixture['session']['id'], safe='')
        with core(executable, data, env) as url:
            caps = request(url, 'GET', '/api/v1/capabilities')
            if caps['candidate']['mode'] != 'live' or caps['features']['analog_audio_inputs']:
                raise RuntimeError('unexpected runtime profile')
            request(url, 'POST', '/api/v1/sessions', fixture['session'])
            request(url, 'POST', route + '/start', {})
            workers = request(url, 'GET', '/api/v1/workers')
            if any(w['alive'] for w in workers): raise RuntimeError('session implicitly opened a device')
            for resource, key in [('sweeps', 'sweep'), ('observations', 'observation')]:
                request(url, 'POST', route + '/' + resource, fixture[key])
        restored = args.report_dir / 'restored'; restored.mkdir()
        # Cold SQLite backup is deliberate: production online manifest backup
        # is separately verified by the shared Rust release suite.
        with sqlite3.connect(data/'sessions.sqlite3') as source, sqlite3.connect(restored/'sessions.sqlite3') as target:
            source.backup(target)
        with core(executable, restored, env) as url:
            for resource, key in [('sweeps', 'sweep'), ('observations', 'observation')]:
                rows = request(url, 'GET', route + '/' + resource + '?limit=100')
                if len(rows) != 1: raise RuntimeError('restored source count mismatch')
                expected = fixture[key]
                fields = ['id', 'session_id', 'stream_id', 'interval', 'frequencies_hz', 'levels', 'level_unit'] if key == 'sweep' else ['id', 'frequency_hz', 'level', 'level_unit', 'interval']
                if key == 'sweep' and [struct.pack('>d', x) for x in rows[0]['levels']] != [struct.pack('>d', x) for x in expected['levels']]:
                    raise RuntimeError('restored float bits differ')
                for field in fields:
                    if rows[0].get(field) != expected.get(field): raise RuntimeError('restored original field differs: ' + field)
        outcome['successful'] = True
    except BaseException as error:
        outcome['error'] = str(error)
        raise
    finally:
        (args.report_dir/'report.json').write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__': main()
