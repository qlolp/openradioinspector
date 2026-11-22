"""Production Field release policy and acceptance checks, without credentials or device access."""
import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile, ZipInfo

from tools.field import android_release as field
from tools.field import android_upgrade
from tools.field import play_distribution as release
from tools.release.macos_rc_package import digest, write_json

PIN = "ab" * 32
UPLOAD = "cd" * 32
MANIFEST = """<manifest xmlns:android="http://schemas.android.com/apk/res/android"
package="org.openradioinspector.field" android:versionCode="7" android:versionName="0.1.0">
<uses-sdk android:minSdkVersion="31" android:targetSdkVersion="36" />
<application android:allowBackup="false" /></manifest>"""
SIGNER = f"""Verified using v2 scheme (APK Signature Scheme v2): true
Signer #1 certificate DN: CN=Field
Signer #1 certificate SHA-256 digest: {PIN}
"""


def sample_receipt():
    def unique(label):
        return hashlib.sha256(label.encode()).hexdigest()
    build = dict(source_revision="a" * 40, version_code=7, aab_sha256="b" * 64, build_run_id=19)
    receipt = dict(**build, apk_sha256="c" * 64, previous_code=6, previous_apk_sha256="d" * 64,
                   model="SM-S908B", android_api=31,
                   clean_offline_install=dict(passed=True, report_sha256=unique("offline")), bench={})
    for direction in ("play_to_apk", "apk_to_play"):
        receipt[direction] = dict(from_code=6, to_code=7, before_session_sha256=unique(direction + "session"),
                                  after_session_sha256=unique(direction + "session"),
                                  same_first_install_time=True, report_sha256=unique(direction))
    for device in field.BENCH_DEVICES:
        receipt["bench"][device] = dict(passed=True, seconds=7200, report_sha256=unique(device))
    return build, receipt


class FieldReleaseTests(unittest.TestCase):
    def test_run_version_and_pins(self):
        props = "fieldVersionName=0.1.0\nfieldVersionCodeBase=1\n"
        self.assertEqual(field.version(props, 6), ("0.1.0", 7))
        for count in (0, field.MAX_CODE, True):
            with self.assertRaises(ValueError):
                field.version(props, count)
        self.assertEqual(field.fingerprint(":".join(["AB"] * 32)), PIN)
        with patch.dict(os.environ, FIELD_PLAY_CERT_SHA256=PIN, FIELD_UPLOAD_CERT_SHA256=PIN):
            with self.assertRaises(ValueError):
                field.signing_pins()

    def test_manifest_identity_offline_and_standalone(self):
        field.check_manifest(MANIFEST, "0.1.0", 7)
        cases = [("org.openradioinspector.field", "org.openradioinspector.field.debug"),
                 ('versionCode="7"', 'versionCode="6"'), ('"0.1.0"', '"0.1.1"'),
                 ("<application ", '<application android:debuggable="true" '),
                 ("<application ", '<application android:testOnly="true" '),
                 ("<manifest ", '<manifest split="config.arm64_v8a" '),
                 ("<application ", '<application android:isSplitRequired="true" '),
                 ("<application ", '<uses-permission android:name="android.permission.INTERNET" /><application '),
                 ("<application ", '<uses-permission-sdk-23 android:name="android.permission.ACCESS_BACKGROUND_LOCATION" /><application ')]
        for old, new in cases:
            with self.subTest(new=new), self.assertRaises(ValueError):
                field.check_manifest(MANIFEST.replace(old, new), "0.1.0", 7)

    def test_apk_signer(self):
        field.check_apk_signer(SIGNER, PIN)
        for report in (SIGNER.replace(PIN, UPLOAD), SIGNER.replace("CN=Field", "CN=Android Debug"),
                       SIGNER.replace(": true", ": false"), SIGNER + f"Signer #2 certificate SHA-256 digest: {PIN}\n"):
            with self.assertRaises(ValueError):
                field.check_apk_signer(report, PIN)

    def test_only_google_universal_with_one_key(self):
        entry = dict(certificateSha256Hash=PIN, generatedUniversalApk={"downloadId": "opaque/+id"})
        self.assertEqual(release.universal_id({"generatedApks": [entry]}, PIN), "opaque%2F%2Bid")
        encoded = base64.b64encode(bytes.fromhex(PIN)).decode()
        self.assertEqual(release.universal_id({"generatedApks": [dict(entry, certificateSha256Hash=encoded)]}, PIN), "opaque%2F%2Bid")
        for groups in ([], [dict(certificateSha256Hash=PIN, generatedSplitApks=[{"downloadId": "base"}])],
                       [dict(entry, unprotectedGeneratedSplitApks=[{}])],
                       [dict(entry, certificateSha256Hash=UPLOAD)], [entry, entry]):
            with self.assertRaises(ValueError):
                release.universal_id({"generatedApks": groups}, PIN)

    def test_play_digest_and_public_rollout(self):
        bundles = {"bundles": [dict(versionCode=7, sha256=PIN)]}
        def track(name, status):
            return {"tracks": [dict(track=name, releases=[dict(status=status, versionCodes=["7"])])]}
        release.check_inventory(bundles, track("internal", "completed"), 7, PIN, False)
        release.check_inventory(bundles, track("production", "completed"), 7, PIN, True)
        for tracks, code, sha in [(track("internal", "completed"), 7, PIN), (track("production", "inProgress"), 7, PIN),
                                  (track("production", "completed"), 6, PIN), (track("production", "completed"), 7, UPLOAD)]:
            with self.assertRaises(ValueError):
                release.check_inventory(bundles, tracks, code, sha, True)
        retained = track("production", "completed")
        retained["tracks"][0]["releases"][0]["versionCodes"].append("8")
        with self.assertRaises(ValueError):
            release.check_inventory(bundles, retained, 7, PIN, True)

    def test_both_upgrade_directions_and_real_report_hashes(self):
        build, good = sample_receipt()
        field.validate_receipt(good, build, good["apk_sha256"])
        for path, replacement in [(("previous_code",), 7), (("build_run_id",), True),
                                  (("source_revision",), "f" * 40), (("apk_sha256",), PIN),
                                  (("play_to_apk", "same_first_install_time"), False),
                                  (("apk_to_play", "after_session_sha256"), PIN),
                                  (("bench", "AOR AR-DV1", "seconds"), 7199),
                                  (("apk_to_play", "report_sha256"), good["play_to_apk"]["report_sha256"])]:
            _, changed = sample_receipt()
            row = changed
            for key in path[:-1]:
                row = row[key]
            row[path[-1]] = replacement
            with self.subTest(path=path), self.assertRaises(ValueError):
                field.validate_receipt(changed, build, good["apk_sha256"])

    def test_workflow_identity_skipped_or_pr(self):
        good = dict(path=".github/workflows/android-field.yml", event="workflow_dispatch", head_branch="main",
                    head_repository={"full_name": "owner/repo"}, head_sha="0" * 40,
                    status="completed", conclusion="success")
        jobs = {"total_count": 2, "jobs": [dict(name=n, conclusion="success") for n in ("field", "macos-field-reader")]}
        release.check_run(good, jobs, "owner/repo", release.TESTS)
        for key, change in [("event", "pull_request"), ("head_repository", {"full_name": "fork/repo"}),
                            ("head_branch", "test"), ("path", ".github/workflows/android-field-bundle.yml")]:
            with self.assertRaises(ValueError):
                release.check_run(dict(good, **{key: change}), jobs, "owner/repo", release.TESTS)
        jobs["jobs"][0]["conclusion"] = "skipped"
        with self.assertRaises(ValueError):
            release.check_run(good, jobs, "owner/repo", release.TESTS)

    def test_version_history_excludes_desktop_and_requires_frozen_apk(self):
        row = dict(tag_name="android-field-v0.1.0", draft=False, immutable=True,
                   assets=[dict(name="OpenRadioInspector-Field-0.1.0-12-universal.apk")])
        desktop = dict(tag_name="v0.4.0", draft=False)
        self.assertEqual(12, release.prior_public_code([desktop, row], "android-field-v0.2.0"))
        with self.assertRaises(ValueError):
            release.prior_public_code([dict(row, immutable=False)], "android-field-v0.2.0")

    def test_upgrade_observations_bind_device_store_and_exact_apk(self):
        old = dict(device_user_sha256="9" * 64, model="SM-S908U1", api=33, version_code=6,
                   first_install_time="2026-01-01 12:01:00", installer="com.android.vending", play_cert_sha256=PIN,
                   installed_base_sha256="1" * 64, split_count=3, session_sha256="2" * 64)
        current = dict(old, version_code=7, installed_base_sha256="3" * 64, split_count=0)
        tx = dict(device_user_sha256=old["device_user_sha256"], first_install_time=old["first_install_time"],
                  from_code=6, to_code=7, apk_sha256="3" * 64, adb_install_r=True)
        def compare(a, b, direction="play_to_apk", transaction=tx):
            return android_upgrade.compare(a, b, direction, 6, 7, "1" * 64, "3" * 64, PIN, transaction)
        self.assertEqual(compare(old, current)["to_code"], 7)
        self.assertEqual(compare(dict(old, split_count=0, installer="com.android.shell"), current,
                                 "apk_to_play", None)["from_code"], 6)
        for change in [dict(installed_base_sha256=PIN), dict(first_install_time="later"),
                       dict(device_user_sha256=PIN), dict(play_cert_sha256=UPLOAD), dict(split_count=1),
                       dict(version_code=6), dict(session_sha256=PIN)]:
            with self.assertRaises(ValueError):
                compare(old, dict(current, **change))
        with self.assertRaises(ValueError):
            compare(dict(old, installer="com.android.shell"), current)
        with self.assertRaises(ValueError):
            compare(dict(old, split_count=0), current, "apk_to_play", None)
        for wrong in (None, dict(tx, apk_sha256=PIN), dict(tx, from_code=7), dict(tx, adb_install_r=False)):
            with self.assertRaises(ValueError):
                compare(old, current, transaction=wrong)

    def test_sideload_installs_only_after_export_and_identity_checks(self):
        before = dict(installer="com.android.vending", version_code=6, first_install_time="original",
                      device_user_sha256=PIN, session_sha256=PIN)
        with tempfile.TemporaryDirectory() as tmp, patch.object(android_upgrade, "observe", return_value=before), \
                patch.object(android_upgrade, "verify_apk") as verify, patch.object(android_upgrade.subprocess, "run") as adb, \
                patch.dict(os.environ, FIELD_PLAY_CERT_SHA256=PIN, FIELD_UPLOAD_CERT_SHA256=UPLOAD):
            apk = Path(tmp) / "candidate.apk"
            apk.write_bytes(b"fixture")
            for snapshot, code in [(before, 6), (dict(before, session_sha256=UPLOAD), 7)]:
                with self.assertRaises(ValueError):
                    android_upgrade.sideload_update("serial", "export", snapshot, "0.1.0", code, apk)
                adb.assert_not_called()
            verify.side_effect = ValueError("wrong certificate")
            with self.assertRaises(ValueError):
                android_upgrade.sideload_update("serial", "export", before, "0.1.0", 7, apk)
            adb.assert_not_called()
            verify.side_effect = None
            adb.return_value = Mock(returncode=0, stdout="Performing Streamed Install\nSuccess\n")
            proof = android_upgrade.sideload_update("serial", "export", before, "0.1.0", 7, apk)
            self.assertEqual(adb.call_args.args[0], ["adb", "-s", "serial", "install", "-r", apk])
            self.assertEqual(proof["apk_sha256"], digest(apk))

    def test_real_session_digest_survives_zip_repack(self):
        original = field.ROOT / "contracts/fixtures/field-android-export-v1.orifield"
        result = field.archive_fingerprint(original)
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "repacked.orifield"
            with ZipFile(original) as src, ZipFile(copy, "w") as dst:
                for name in src.namelist():
                    dst.writestr(ZipInfo(name, date_time=(1981, 1, 1, 0, 0, 0)), src.read(name))
            self.assertNotEqual(digest(original), digest(copy))
            self.assertEqual(field.archive_fingerprint(copy), result)

    def test_play_read_discards_temporary_edit_even_when_track_fails(self):
        with patch.dict(os.environ, FIELD_PLAY_TOKEN="fixture"):
            play = release.Play()
        play.request = Mock(side_effect=[{"id": "16"}, {"bundles": []}, {"tracks": []}, None])
        with self.assertRaises(ValueError):
            play.check(7, PIN, public=True)
        self.assertEqual([(c.args[0], c.kwargs.get("method", "GET")) for c in play.request.call_args_list],
                         [("edits", "POST"), ("edits/16/bundles", "GET"), ("edits/16/tracks", "GET"),
                          ("edits/16", "DELETE")])

    def test_publication_requires_review_public_play_and_exact_frozen_bytes(self):
        build, receipt = sample_receipt()
        build.update(version_name="0.1.0", review_revision="f" * 40, application_id=field.PACKAGE,
                     play_cert_sha256=PIN, upload_cert_sha256=UPLOAD)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GITHUB_RUN_ID="123", FIELD_PLAY_CERT_SHA256=PIN,
                                                              FIELD_UPLOAD_CERT_SHA256=UPLOAD):
            out = Path(tmp)
            apk = out / release.apk_name(build)
            apk.write_bytes(b"fixture APK bytes")
            receipt["apk_sha256"] = digest(apk)
            write_json(out / release.RECEIPT, receipt)
            build.update(apk_sha256=digest(apk), apk_bytes=apk.stat().st_size, apk=apk.name,
                         acceptance_sha256=digest(out / release.RECEIPT))
            write_json(out / release.RECORD, build)
            release.write_sums(out)
            accepted = digest(out / "SHA256SUMS")
            repo, play = Mock(), Mock()
            repo.repo, repo.base, repo.revision = "owner/repo", "repos/owner/repo/", build["review_revision"]
            repo.api.return_value = []
            with patch.object(release, "github") as github:
                with self.assertRaisesRegex(ValueError, "review"):
                    release.publish(repo, out, accepted, play)
                play.check.assert_not_called()
                github.assert_not_called()
                repo.api.return_value = [{"state": "approved", "user": {"type": "User"},
                                         "environments": [{"name": "android-field-publication"}]}]
                play.check.side_effect = ValueError("not public")
                with self.assertRaisesRegex(ValueError, "not public"):
                    release.publish(repo, out, accepted, play)
                play.check.assert_called_once_with(7, build["aab_sha256"], public=True)
                github.assert_not_called()
                play.check.side_effect = None
                apk.write_bytes(b"replaced")
                with self.assertRaisesRegex(ValueError, "checksums"):
                    release.publish(repo, out, accepted, play)
                github.assert_not_called()
