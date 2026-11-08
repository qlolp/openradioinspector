#!/usr/bin/env python3
"""Exercise a built core executable with private data and a parent EOF lifeline."""
import argparse
from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('executable', type=Path)
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix='ori-frozen-core-') as directory:
    root = Path(directory)
    ready = root/'ready'
    child = subprocess.Popen([str(args.executable.resolve()), '--data-dir', directory,
        '--port', '0', '--ready-file', str(ready), '--stdin-lifeline'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic()+30
        while not ready.exists() and child.poll() is None and time.monotonic()<deadline:
            time.sleep(.05)
        if not ready.exists():
            raise RuntimeError('Frozen core did not confirm startup')
        url = ready.read_text().strip()
        if not url.startswith('http://127.0.0.1:'):
            raise RuntimeError('Frozen readiness must be owned loopback')
        connection = HTTPConnection('127.0.0.1', int(url.rsplit(':',1)[1]), timeout=5)
        try:
            connection.request('GET','/api/v1/health')
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read())['data']['status'] == 'ok'
        finally:
            connection.close()
        child.stdin.close()
        child.stdin = None
        output, error = child.communicate(timeout=30)
        if child.returncode != 0:
            raise RuntimeError(error.decode('utf-8',errors='replace'))
        if ready.exists() or not (root/'sessions.sqlite3').exists():
            raise RuntimeError('Frozen core did not clean readiness/preserve archive')
        print('Frozen owned core readiness, EOF shutdown and archive retention passed')
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
