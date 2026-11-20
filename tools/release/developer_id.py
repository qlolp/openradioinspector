"""Sign inside out with a provisioned Developer ID Application identity and notarytool profile."""
import json
import os
import re
import subprocess
import uuid


def command(*args):
    return subprocess.run(list(map(str, args)), check=True, text=True, stdout=subprocess.PIPE).stdout


def sign(path, identity, *, runtime):
    if not re.fullmatch('[A-Fa-f0-9]{40}', identity):
        raise ValueError('configure the Developer ID Application certificate fingerprint')
    command('/usr/bin/codesign', '--force', '--sign', identity, '--timestamp',
            *(['--options', 'runtime'] if runtime else []), path)


def verify_code(path, team, *, runtime):
    if not re.fullmatch('[A-Z0-9]{10}', team):
        raise ValueError('configure the Apple developer team identifier')
    requirement = f'anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = "{team}"'
    command('/usr/bin/codesign', '--verify', '--strict', '-R=' + requirement, path)
    detail = subprocess.run(['/usr/bin/codesign', '-dv', '--verbose=4', str(path)], check=True,
                            text=True, stderr=subprocess.PIPE).stderr
    if not re.search(r'^Timestamp=.+$', detail, re.M) or runtime and not re.search(r'flags=0x[\da-f]+\([^)]*runtime', detail):
        raise ValueError('secure timestamp or hardened runtime missing from signed code')


def notarize(payload, profile):
    if not profile:
        raise ValueError('configure a notarytool keychain profile on the signer')
    result = json.loads(command('/usr/bin/xcrun', 'notarytool', 'submit', payload, '--keychain-profile',
                                profile, *(['--keychain', os.environ['ORI_NOTARY_KEYCHAIN']] if os.getenv('ORI_NOTARY_KEYCHAIN') else []),
                                '--wait', '--output-format', 'json'))
    if result.get('status') != 'Accepted' or not isinstance(result.get('id'), str):
        raise ValueError('Apple did not accept the notarization submission')
    return str(uuid.UUID(result['id']))


def staple(path):
    command('/usr/bin/xcrun', 'stapler', 'staple', path)
    command('/usr/bin/xcrun', 'stapler', 'validate', path)
