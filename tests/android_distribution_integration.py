"""Build and verify a Field fixture APK using the official Android SDK and disposable signing keys."""
import base64
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from zipfile import ZipFile

from tools.field import android_release as field


class RealPackageTests(unittest.TestCase):
    def test_signed_bundle_and_production_apk_with_throwaway_certificates(self):
        aab = Path(os.environ["FIELD_TEST_AAB"]).resolve()
        jar = Path(os.environ["FIELD_TEST_BUNDLETOOL"]).resolve()
        name, _ = field.version((field.ROOT / "clients/android/gradle.properties").read_text(), 1)
        with tempfile.TemporaryDirectory() as folder:
            scratch = Path(folder)
            def run(*cmd, **kwargs):
                return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
            def make_key(alias):
                keystore = scratch / (alias + ".p12")
                run("keytool", "-genkeypair", "-alias", alias, "-keystore", keystore, "-storepass", "fixturepass",
                    "-keypass", "fixturepass", "-keyalg", "RSA", "-keysize", "2048", "-validity", "3650",
                    "-dname", "CN=Fixture " + alias)
                cert = scratch / (alias + ".der")
                run("keytool", "-exportcert", "-alias", alias, "-keystore", keystore,
                    "-storepass", "fixturepass", "-file", cert)
                return keystore, hashlib.sha256(cert.read_bytes()).hexdigest()
            store, upload = make_key("upload")
            signing, play = make_key("play")
            env = dict(os.environ, FIELD_UPLOAD_KEYSTORE_B64=base64.b64encode(store.read_bytes()).decode(),
                       FIELD_UPLOAD_ALIAS="upload", FIELD_UPLOAD_STORE_PASSWORD="fixturepass",
                       FIELD_UPLOAD_KEY_PASSWORD="fixturepass", FIELD_UPLOAD_CERT_SHA256=upload, FIELD_PLAY_CERT_SHA256=play)
            signed = scratch / "signed.aab"
            run("bash", "clients/android/ci/sign-upload.sh", aab, signed, env=env, cwd=field.ROOT)
            field.verify_bundle(signed, jar, name, 1)
            with self.assertRaises(subprocess.CalledProcessError):
                run("bash", "clients/android/ci/sign-upload.sh", aab, scratch / "wrong.aab",
                    env=dict(env, FIELD_UPLOAD_CERT_SHA256=play), cwd=field.ROOT)
            apks = scratch / "universal.apks"
            run("java", f"-Djava.io.tmpdir={scratch}", "-jar", jar, "build-apks", "--mode=universal", f"--bundle={signed}", f"--output={apks}",
                f"--ks={signing}", "--ks-pass=pass:fixturepass", "--ks-key-alias=play")
            with ZipFile(apks) as archive:
                path = Path(archive.extract("universal.apk", scratch))
            field.verify_apk(path, name, 1, play)
            with self.assertRaises(ValueError):
                field.verify_apk(path, name, 1, upload)
            with self.assertRaises(ValueError):
                field.verify_apk(path, name, 2, play)
            debug = Path(os.environ["FIELD_TEST_DEBUG_APK"]).resolve()
            apksigner = Path(os.environ["ANDROID_HOME"]) / "build-tools" / field.SDK_TOOLS / "apksigner"
            report = run(apksigner, "verify", "--print-certs", debug).stdout.decode()
            debug_pin = re.search(r"^Signer #1 certificate SHA-256 digest: ([0-9a-f]{64})$", report, re.M).group(1)
            with self.assertRaises(ValueError):
                field.verify_apk(debug, name, 1, debug_pin)
