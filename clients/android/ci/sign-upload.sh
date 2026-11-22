#!/usr/bin/env bash
# Sign a copy of the verified AAB with the publisher's upload key.
set -euo pipefail
umask 077
input=$1
output=$2
test ! -e "$output"
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
printf '%s' "$FIELD_UPLOAD_KEYSTORE_B64" | base64 --decode > "$scratch/upload.keystore"
unset FIELD_UPLOAD_KEYSTORE_B64
keytool -exportcert -keystore "$scratch/upload.keystore" -storepass:env FIELD_UPLOAD_STORE_PASSWORD \
  -alias "$FIELD_UPLOAD_ALIAS" -file "$scratch/upload.der"
python3 - "$scratch/upload.der" <<'PY'
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from tools.field.android_release import fingerprint, signing_pins
from tools.release.release_artifacts import require

# A new Play app can receive its Google certificate only after its first upload.
if not os.environ.get("FIELD_PLAY_CERT_SHA256") and os.environ.get("FIELD_PLAY_MAX_VERSION_CODE") == "0":
    upload = fingerprint(os.environ["FIELD_UPLOAD_CERT_SHA256"])
else:
    _, upload = signing_pins()
cert = Path(sys.argv[1])
require(hashlib.sha256(cert.read_bytes()).hexdigest() == upload, "wrong upload certificate")
subject = subprocess.check_output(["openssl", "x509", "-inform", "DER", "-in", cert,
                                   "-noout", "-subject", "-nameopt", "RFC2253"], text=True)
require(not re.search(r"\bCN\s*=\s*Android Debug\b", subject, re.I), "debug upload key")
PY
jarsigner -keystore "$scratch/upload.keystore" -storepass:env FIELD_UPLOAD_STORE_PASSWORD \
  -keypass:env FIELD_UPLOAD_KEY_PASSWORD -digestalg SHA-256 -signedjar "$scratch/signed.aab" \
  "$input" "$FIELD_UPLOAD_ALIAS"
# Strict verification checks every payload entry and the selected trusted alias.
jarsigner -verify -strict -keystore "$scratch/upload.keystore" -storepass:env FIELD_UPLOAD_STORE_PASSWORD \
  "$scratch/signed.aab" "$FIELD_UPLOAD_ALIAS"
mv -- "$scratch/signed.aab" "$output"
