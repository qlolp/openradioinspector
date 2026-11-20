"""Provision and remove Apple credentials on an ephemeral hosted release runner."""
import base64
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile


def security(*args):
    # In particular never include security's password arguments in a raised
    # CalledProcessError, in subprocess output or in a logged command line.
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if result.returncode:
        raise ValueError(f'{Path(args[0]).name} {args[1]} failed on the signing runner')
    return result.stdout


def prepare(root):
    if not os.getenv('ORI_MAC_P12'):
        return  # Dedicated runner with a preprovisioned identity and notary profile.
    keychain = root / 'ori-distribution.keychain-db'
    state = root / 'ori-distribution-keychains.json'
    if keychain.exists() or state.exists():
        raise ValueError('signing keychain workspace is already occupied')
    original = shlex.split(security('/usr/bin/security', 'list-keychains', '-d', 'user'))
    with state.open('x', encoding='utf-8') as out:
        json.dump(original, out)
    password = secrets.token_hex(32)
    security('/usr/bin/security', 'create-keychain', '-p', password, str(keychain))
    security('/usr/bin/security', 'set-keychain-settings', '-lut', '10800', str(keychain))
    security('/usr/bin/security', 'unlock-keychain', '-p', password, str(keychain))
    security('/usr/bin/security', 'list-keychains', '-d', 'user', '-s', str(keychain), *original)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        p12 = Path(tmp) / 'identity.p12'
        p12.write_bytes(base64.b64decode(os.environ['ORI_MAC_P12'], validate=True))
        security('/usr/bin/security', 'import', str(p12), '-k', str(keychain), '-P', os.environ['ORI_MAC_P12_PASSWORD'],
                 '-f', 'pkcs12', '-T', '/usr/bin/codesign')
        security('/usr/bin/security', 'set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:', '-s',
                 '-k', password, str(keychain))
        p8 = Path(tmp) / 'notary.p8'
        p8.write_bytes(base64.b64decode(os.environ['ORI_NOTARY_P8'], validate=True))
        security('/usr/bin/xcrun', 'notarytool', 'store-credentials', 'ori-release', '--keychain', str(keychain),
                 '--key', str(p8), '--key-id', os.environ['ORI_NOTARY_KEY_ID'], '--issuer', os.environ['ORI_NOTARY_ISSUER'])
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as out:
        out.write(f'ORI_NOTARY_KEYCHAIN={keychain}\nORI_HOSTED_NOTARY_PROFILE=ori-release\n')


def cleanup(root):
    state = root / 'ori-distribution-keychains.json'
    if state.exists():
        keychain = root / 'ori-distribution.keychain-db'
        original = json.loads(state.read_bytes())
        security('/usr/bin/security', 'list-keychains', '-d', 'user', '-s', *original)
        if keychain.exists():
            security('/usr/bin/security', 'delete-keychain', str(keychain))
        state.unlink()


if __name__ == '__main__':
    os.umask(0o077)
    {'prepare': prepare, 'cleanup': cleanup}[sys.argv[1]](Path(os.environ['RUNNER_TEMP']))
