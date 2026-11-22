"""Verify production Field packages and the public projection of installed Android tests."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZIP_STORED, ZipFile

from tools.field import android_notices
from tools.release.macos_rc_package import digest
from tools.release.release_artifacts import keys, require, sha

PACKAGE = "org.openradioinspector.field"
PLAY_URL = "https://play.google.com/store/apps/details?id=" + PACKAGE
ANDROID = "{http://schemas.android.com/apk/res/android}"
SDK_TOOLS = "36.0.0"
MAX_CODE = 2_100_000_000
BENCH_DEVICES = ("RF Explorer 3G Combo Standard", "tinySA Ultra", "AOR AR-DV1")
ROOT = Path(__file__).resolve().parents[2]


def version(properties, run_number):
    props = dict(line.split("=", 1) for line in properties.splitlines()
                 if line.strip() and not line.startswith("#") and "=" in line)
    name = props["fieldVersionName"]
    require(re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", name),
            "expected a three-part Field versionName")
    base = int(props["fieldVersionCodeBase"])
    require(type(run_number) is int and run_number > 0 and 0 <= base < MAX_CODE
            and base + run_number <= MAX_CODE, "invalid production versionCode")
    return name, base + run_number


def fingerprint(value):
    require(isinstance(value, str), "missing SHA-256 certificate pin")
    value = value.strip()
    require(re.fullmatch(r"(?:[a-fA-F0-9]{64}|(?:[a-fA-F0-9]{2}:){31}[a-fA-F0-9]{2})", value),
            "expected a 32-byte hex SHA-256 certificate pin")
    return value.replace(":", "").lower()


def signing_pins():
    play = fingerprint(os.environ["FIELD_PLAY_CERT_SHA256"])
    upload = fingerprint(os.environ["FIELD_UPLOAD_CERT_SHA256"])
    require(play != upload, "Play and upload certificates must be distinct")
    return play, upload


def check_manifest(xml, name, code):
    root = ET.fromstring(xml)
    app, sdk = root.find("application"), root.find("uses-sdk")
    require(root.tag == "manifest" and root.get("package") == PACKAGE
            and int(root.get(ANDROID + "versionCode", "0")) == code
            and (name is None or root.get(ANDROID + "versionName") == name),
            "unexpected Field package name or version")
    require(app is not None and sdk is not None and sdk.get(ANDROID + "minSdkVersion") == "31"
            and sdk.get(ANDROID + "targetSdkVersion") == "36", "unexpected Android SDK range")
    require(all(app.get(ANDROID + flag, "false") == "false" for flag in ("debuggable", "testOnly"))
            and app.get(ANDROID + "allowBackup") == "false", "debug, test or backup-enabled application")
    require(not root.get("split") and root.find("uses-split") is None
            and app.get(ANDROID + "isSplitRequired", "false") == "false"
            and not root.get(ANDROID + "requiredSplitTypes")
            and not any(meta.get(ANDROID + "name") == "com.android.vending.splits.required"
                        and meta.get(ANDROID + "value") != "false" for meta in app.findall("meta-data")),
            "APK requires additional splits")
    permissions = {item.get(ANDROID + "name") for item in root if item.tag.startswith("uses-permission")}
    require(not permissions & {"android.permission.INTERNET", "android.permission.ACCESS_BACKGROUND_LOCATION"},
            "forbidden network or background location permission")


def verify_native(path):
    subprocess.run(["python3", str(ROOT / "clients/android/ci/verify-packages.py"), path], check=True, stdout=sys.stderr)


def verify_bundle(path, jar, name, code):
    def bundletool(*args):
        return subprocess.check_output(["java", "-jar", jar, *args, f"--bundle={path}"], text=True)
    bundletool("validate")
    require("PAGE_ALIGNMENT_16K" in bundletool("dump", "config"), "bundle lacks 16 KiB alignment")
    check_manifest(bundletool("dump", "manifest", "--module=base"), name, code)
    with ZipFile(path) as bundle:
        manifests = [n for n in bundle.namelist() if n.endswith("/manifest/AndroidManifest.xml")]
        require(manifests == ["base/manifest/AndroidManifest.xml"], "expected a self-contained base module")
    verify_native(path)
    android_notices.verify_package(path, (android_notices.SOURCES / "inventory.sha256").read_text().strip())


def check_apk_signer(report, play_pin):
    certs = re.findall(r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]+)$", report, re.M)
    require(len(certs) == 1 and fingerprint(certs[0]) == play_pin
            and re.search(r"^Verified using v[23](?:\.1)? scheme .*: true$", report, re.M)
            and not re.search(r"\bCN\s*=\s*Android Debug\b", report, re.I),
            "expected one modern APK signature from the pinned Play app-signing certificate")


def verify_apk(path, name, code, play_pin, notice_digest=None):
    sdk = Path(os.environ["ANDROID_HOME"])
    tools = sdk / "build-tools" / SDK_TOOLS
    report = subprocess.check_output([tools / "apksigner", "verify", "--verbose", "--print-certs", path], text=True)
    check_apk_signer(report, play_pin)
    xml = subprocess.check_output([sdk / "cmdline-tools/latest/bin/apkanalyzer", "manifest", "print", path],
                                  text=True)
    check_manifest(xml, name, code)
    subprocess.run([tools / "zipalign", "-c", "-P", "16", "4", path], check=True, stdout=sys.stderr)
    with ZipFile(path) as apk:
        require(all(entry.compress_type == ZIP_STORED for entry in apk.infolist()
                    if entry.filename.startswith("lib/") and entry.filename.endswith(".so")),
                "compressed native library in the universal APK")
    verify_native(path)
    if name is not None:
        expected = notice_digest or (android_notices.SOURCES / "inventory.sha256").read_text().strip()
        android_notices.verify_package(path, expected)


def archive_fingerprint(path):
    from tools.field_archive import validate
    manifest = validate(path)
    require(manifest["sweep_count"] + manifest["observation_count"] > 0, "test session needs a measurement")
    # The ZIP timestamps can change on re-export. The verified manifest binds every payload and source ID.
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def validate_receipt(receipt, build, apk_sha):
    common = ("source_revision", "version_code", "aab_sha256", "build_run_id")
    keys(receipt, (*common, "apk_sha256", "previous_code", "previous_apk_sha256", "model", "android_api", "clean_offline_install",
                   "play_to_apk", "apk_to_play", "bench"), "Field acceptance")
    require(all(type(receipt[k]) is type(build[k]) and receipt[k] == build[k] for k in common)
            and receipt["apk_sha256"] == apk_sha and sha(apk_sha), "receipt belongs to a different candidate")
    require(type(receipt["previous_code"]) is int and 0 < receipt["previous_code"] < build["version_code"]
            and sha(receipt["previous_apk_sha256"]) and receipt["previous_apk_sha256"] != apk_sha,
            "a distinct, lower Play-signed version is required")
    require(isinstance(receipt["model"], str) and re.fullmatch(r"SM-S908[A-Z0-9]+", receipt["model"])
            and type(receipt["android_api"]) is int and receipt["android_api"] >= 31, "S22 Ultra device evidence required")
    clean = receipt["clean_offline_install"]
    keys(clean, ("passed", "report_sha256"), "clean offline install")
    require(clean["passed"] is True, "clean offline installation failed")
    reports = [clean["report_sha256"]]
    for direction in ("play_to_apk", "apk_to_play"):
        item = receipt[direction]
        keys(item, ("from_code", "to_code", "before_session_sha256", "after_session_sha256", "same_first_install_time",
                    "report_sha256"), direction)
        require(type(item["from_code"]) is int and type(item["to_code"]) is int
                and item["from_code"] == receipt["previous_code"] and item["to_code"] == build["version_code"]
                and sha(item["before_session_sha256"]) and item["before_session_sha256"] == item["after_session_sha256"]
                and item["same_first_install_time"] is True, f"failed in-place upgrade: {direction}")
        reports.append(item["report_sha256"])
    keys(receipt["bench"], BENCH_DEVICES, "Galaxy S22 Ultra bench")
    for device in BENCH_DEVICES:
        item = receipt["bench"][device]
        keys(item, ("passed", "seconds", "report_sha256"), device)
        require(item["passed"] is True and type(item["seconds"]) is int and item["seconds"] >= 7200,
                f"incomplete S22 run: {device}")
        reports.append(item["report_sha256"])
    require(all(sha(h) for h in reports) and len(reports) == len(set(reports)),
            "distinct, successful installed-test report digests required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ver = sub.add_parser("version")
    ver.add_argument("run_number", type=int)
    ver.add_argument("max_uploaded_code", type=int)
    for cmd in ("verify-bundle", "verify-apk"):
        p = sub.add_parser(cmd)
        p.add_argument("artifact", type=Path)
        p.add_argument("name")
        p.add_argument("code", type=int)
        if cmd == "verify-bundle":
            p.add_argument("bundletool", type=Path)
    session = sub.add_parser("session-hash")
    session.add_argument("archive", type=Path)
    receipt = sub.add_parser("receipt-template", help="write a template bound to the provided artifact bytes")
    receipt.add_argument("revision")
    receipt.add_argument("build_run", type=int)
    receipt.add_argument("code", type=int)
    receipt.add_argument("aab", type=Path)
    receipt.add_argument("apk", type=Path)
    receipt.add_argument("previous_code", type=int)
    receipt.add_argument("previous_apk", type=Path)
    args = parser.parse_args()
    if args.command == "version":
        name, code = version((ROOT / "clients/android/gradle.properties").read_text(), args.run_number)
        require(0 <= args.max_uploaded_code < code, "versionCode already allocated in Play")
        print(f"name={name}\ncode={code}")
    elif args.command == "verify-apk":
        play, _ = signing_pins()
        verify_apk(args.artifact, args.name, args.code, play)
        print(f"{digest(args.artifact)}  {args.artifact.name}")
    elif args.command == "verify-bundle":
        verify_bundle(args.artifact, args.bundletool, args.name, args.code)
    elif args.command == "session-hash":
        print(archive_fingerprint(args.archive))
    else:
        require(sha(args.revision, 40) and args.build_run > 0 and 0 < args.previous_code < args.code <= MAX_CODE,
                "supply the signed source, run, and a lower previous versionCode")
        row = dict(source_revision=args.revision, build_run_id=args.build_run, version_code=args.code,
                   aab_sha256=digest(args.aab), apk_sha256=digest(args.apk), previous_code=args.previous_code,
                   previous_apk_sha256=digest(args.previous_apk), model=None, android_api=None,
                   clean_offline_install=dict(passed=False, report_sha256=None), play_to_apk=None, apk_to_play=None,
                   bench={d: dict(seconds=None, passed=False, report_sha256=None) for d in BENCH_DEVICES})
        print(json.dumps(row, indent=2))


if __name__ == "__main__":
    main()
