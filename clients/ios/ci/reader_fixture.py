"""Run the built iOS test bundle against a disposable loopback Rust replay core."""

import argparse
import base64
import contextlib
import http.client
import json
import os
from pathlib import Path
import plistlib
import socket
import ssl
import subprocess
import tempfile
import time
from urllib.parse import quote, urlsplit


ROOT = Path(__file__).resolve().parents[3]


def code_for(directory, scope):
    # The installation's owner-only socket is inside this private temporary directory.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(directory / '.ori-runtime/core-admin.sock'))
        connection.sendall(json.dumps({'version': 1, 'command': 'pairing.start', 'scope': scope,
                                       'ttl_seconds': 300, 'attempts': 5}).encode())
        connection.shutdown(socket.SHUT_WR)
        with connection.makefile('rb') as response:
            body = json.loads(response.read(4097))
    assert body['access_scope'] == scope
    return body['pairing_code']


def request(origin, context, path, body=None, token=None):
    parts = urlsplit(origin)
    connection = http.client.HTTPSConnection(parts.hostname, parts.port, context=context, timeout=10)
    try:
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        connection.request('GET' if body is None else 'POST', '/api/v1' + path,
                           None if body is None else json.dumps(body).encode(), headers)
        response = connection.getresponse()
        if response.status not in (200, 201):
            raise RuntimeError(f'Synthetic fixture setup returned HTTP {response.status} at {path}')
        return json.load(response)['data']
    finally:
        connection.close()


def encoded_ca(directory):
    pem = (directory / '.ori-runtime/security/public-ca.pem').read_text()
    return base64.b64encode(ssl.PEM_cert_to_DER_cert(pem)).decode()


@contextlib.contextmanager
def replay(executable):
    # A short path also fits the Darwin sockaddr_un limit.
    with tempfile.TemporaryDirectory(prefix='ori-ios-', dir='/tmp') as temp:
        root = Path(temp)
        data, alternate = root / 'core', root / 'other'
        for item in (data, alternate):
            item.mkdir(mode=0o700)
            subprocess.run([executable, 'init-lan', '--data-dir', item, '--name', '127.0.0.1'], check=True,
                           stdout=subprocess.DEVNULL)
        ready = root / 'ready'
        with subprocess.Popen([executable, '--mode', 'replay', '--data-dir', data, '--lan',
                               '--host', '127.0.0.1', '--port', '0', '--trusted-host', '127.0.0.1:0',
                               '--ready-file', ready, '--stdin-lifeline'], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL) as process:
            try:
                deadline = time.monotonic() + 30
                while not ready.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Synthetic replay core did not become ready')
                    time.sleep(0.05)
                origin = ready.read_text().strip()
                if urlsplit(origin).scheme != 'https' or urlsplit(origin).hostname != '127.0.0.1':
                    raise RuntimeError('Expected private TLS loopback origin')
                context = ssl.create_default_context(cafile=str(data / '.ori-runtime/security/public-ca.pem'))
                operator = request(origin, context, '/security/pair',
                                   {'client': 'native', 'label': 'Disposable test setup', 'required_scope': 'operator',
                                    'pairing_code': code_for(data, 'operator')})['token']
                fixture = json.loads((ROOT / 'contracts/fixtures/measurement-v1.json').read_text())
                assert fixture['synthetic'] is True
                session = '/sessions/' + quote(fixture['session']['id'], safe='')
                for path, body in [('/sessions', fixture['session']), (session + '/start', {}),
                                   (session + '/sweeps', fixture['sweep']),
                                   (session + '/observations', fixture['observation'])]:
                    request(origin, context, path, body, operator)
                request(origin, context, '/security/credentials/current/revoke', {}, operator)
                yield {'origin': origin, 'ca': encoded_ca(data), 'otherCA': encoded_ca(alternate),
                       'code': code_for(data, 'reader')}
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('core', type=Path)
    p.add_argument('xctestrun', type=Path)
    p.add_argument('destination')
    p.add_argument('result', type=Path)
    args = p.parse_args()
    with replay(args.core.resolve()) as fixture:
        plan = plistlib.loads(args.xctestrun.read_bytes())
        # Format 2 test plans group targets under a configuration. Older Xcode also
        # supports named targets at the root. Inject into the unit-test process only.
        if 'TestConfigurations' in plan:
            targets = [target for configuration in plan['TestConfigurations'] for target in configuration['TestTargets']]
        else:
            targets = [value for key, value in plan.items() if key != '__xctestrun_metadata__']
        unit = [target for target in targets if target.get('BlueprintName') == 'FieldTests'
                or target.get('TestBundlePath', '').endswith('/FieldTests.xctest')]
        if len(unit) != 1:
            raise RuntimeError('Expected exactly one FieldTests bundle in the generated test plan')
        unit[0].setdefault('EnvironmentVariables', {})['ORI_READER_FIXTURE'] = json.dumps(fixture)
        # __TESTROOT__ in the original plist is relative to this directory.
        with tempfile.NamedTemporaryFile(suffix='.xctestrun', dir=args.xctestrun.parent) as injected:
            plistlib.dump(plan, injected)
            injected.flush()
            subprocess.run(['xcodebuild', 'test-without-building', '-xctestrun', injected.name,
                            '-destination', args.destination, '-parallel-testing-enabled', 'NO',
                            '-resultBundlePath', args.result], check=True)


if __name__ == '__main__':
    main()
