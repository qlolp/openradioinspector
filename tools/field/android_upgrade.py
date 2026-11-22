"""Observe an installed Field app over USB and compare exports across a real in-place update."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from tools.field.android_release import (PACKAGE, SDK_TOOLS, archive_fingerprint, check_apk_signer, signing_pins, verify_apk)
from tools.release.macos_rc_package import digest, read_json
from tools.release.release_artifacts import require, sha


def observe(serial, archive):
    adb = ["adb", "-s", serial]

    def shell(*args):
        return subprocess.check_output([*adb, "shell", *args], text=True).strip()

    android_user = shell("am", "get-current-user")
    require(android_user.isdecimal(), "expected the active Android user")
    paths = shell("pm", "path", "--user", android_user, PACKAGE).splitlines()
    base = [line.removeprefix("package:") for line in paths if line.startswith("package:") and line.endswith("/base.apk")]
    require(len(base) == 1, "expected exactly one installed Field base APK")
    with tempfile.TemporaryDirectory() as scratch:
        local_apk = Path(scratch) / "base.apk"
        subprocess.run([*adb, "pull", base[0], local_apk], check=True, stdout=subprocess.DEVNULL)
        tools = Path(os.environ["ANDROID_HOME"]) / "build-tools" / SDK_TOOLS
        verified = subprocess.check_output([tools / "apksigner", "verify", "--verbose", "--print-certs", local_apk],
                                           text=True)
        play, _ = signing_pins()
        check_apk_signer(verified, play)
        installed_hash = digest(local_apk)
    packages = shell("pm", "list", "packages", "-i", "--user", android_user, PACKAGE)
    installer = re.search(r"^package:" + re.escape(PACKAGE) + r"\s+installer=(\S+)\s*$", packages, re.M)
    require(installer is not None, "cannot identify Field's installer")
    state = shell("dumpsys", "package", PACKAGE)
    codes = set(re.findall(r"^\s*versionCode=(\d+)\b", state, re.M))
    times = set(re.findall(r"^\s*firstInstallTime=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*$", state, re.M))
    require(len(codes) == 1 and len(times) == 1, "ambiguous package state; use a single-user test device")
    return dict(device_user_sha256=hashlib.sha256((serial + "/" + android_user).encode()).hexdigest(),
                model=shell("getprop", "ro.product.model"), api=int(shell("getprop", "ro.build.version.sdk")),
                version_code=int(codes.pop()), first_install_time=times.pop(), installer=installer.group(1),
                play_cert_sha256=play, installed_base_sha256=installed_hash, split_count=len(paths) - 1,
                session_sha256=archive_fingerprint(archive))


def sideload_update(serial, export, before, name, code, apk):
    require(observe(serial, export) == before and before["installer"] == "com.android.vending"
            and 0 < before["version_code"] < code, "a current, exported, lower Play installation is required")
    play, _ = signing_pins()
    verify_apk(apk, name, code, play)
    apk_sha = digest(apk)
    result = subprocess.run(["adb", "-s", serial, "install", "-r", apk], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    require(result.returncode == 0 and "Success" in result.stdout.splitlines(), "adb install -r: " + result.stdout)
    return dict(device_user_sha256=before["device_user_sha256"], first_install_time=before["first_install_time"],
                from_code=before["version_code"], to_code=code, apk_sha256=apk_sha, adb_install_r=True)


def compare(before, after, direction, previous_code, code, previous_apk, apk, play, transaction=None):
    require(before["device_user_sha256"] == after["device_user_sha256"] and sha(before["device_user_sha256"])
            and before["model"] == after["model"] and before["api"] == after["api"]
            and re.fullmatch(r"SM-S908[A-Z0-9]+", before["model"]) and before["api"] >= 31,
            "both observations must come from the same S22 Ultra user and OS")
    require(before["play_cert_sha256"] == after["play_cert_sha256"] == play
            and before["version_code"] == previous_code < code == after["version_code"],
            "observed code or Play certificate differs")
    require(before["first_install_time"] == after["first_install_time"] and bool(before["first_install_time"])
            and sha(before["session_sha256"]) and before["session_sha256"] == after["session_sha256"],
            "original installation or archived measurements were lost")
    if direction == "play_to_apk":
        store, offline, universal_sha = before, after, apk
        require(isinstance(transaction, dict) and transaction.get("adb_install_r") is True
                and transaction == dict(device_user_sha256=before["device_user_sha256"],
                                    first_install_time=before["first_install_time"], from_code=previous_code,
                                    to_code=code, apk_sha256=apk, adb_install_r=True),
                "missing successful adb install -r transaction for these bytes and installation")
    else:
        require(direction == "apk_to_play" and before["installer"] != "com.android.vending" and transaction is None,
                "APK-to-Play must start from a fresh sideloaded installation")
        store, offline, universal_sha = after, before, previous_apk
    require(store["installer"] == "com.android.vending" and offline["split_count"] == 0
            and sha(universal_sha) and offline["installed_base_sha256"] == universal_sha,
            "expected an actual Play install on one side and the exact universal APK on the other")
    evidence = dict(direction=direction, before=before, after=after, transaction=transaction)
    proof = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return dict(from_code=previous_code, to_code=code, before_session_sha256=before["session_sha256"],
                after_session_sha256=after["session_sha256"], same_first_install_time=True, report_sha256=proof)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    capture = sub.add_parser("observe")
    capture.add_argument("serial")
    capture.add_argument("export", type=Path)
    update = sub.add_parser("sideload-update", help="install a verified higher-code APK over a Play installation")
    update.add_argument("serial")
    update.add_argument("export", type=Path)
    update.add_argument("before", type=Path)
    update.add_argument("name")
    update.add_argument("code", type=int)
    update.add_argument("apk", type=Path)
    upgrade = sub.add_parser("compare")
    upgrade.add_argument("direction", choices=("play_to_apk", "apk_to_play"))
    upgrade.add_argument("before", type=Path)
    upgrade.add_argument("after", type=Path)
    upgrade.add_argument("previous_code", type=int)
    upgrade.add_argument("code", type=int)
    upgrade.add_argument("previous_apk", type=Path)
    upgrade.add_argument("apk", type=Path)
    upgrade.add_argument("--transaction", type=Path)
    args = parser.parse_args()
    if args.cmd == "observe":
        row = observe(args.serial, args.export)
    elif args.cmd == "sideload-update":
        row = sideload_update(args.serial, args.export, read_json(args.before), args.name, args.code, args.apk)
    else:
        play, _ = signing_pins()
        row = compare(read_json(args.before), read_json(args.after), args.direction, args.previous_code,
                      args.code, digest(args.previous_apk), digest(args.apk), play,
                      read_json(args.transaction) if args.transaction else None)
    print(json.dumps(row, indent=2))


if __name__ == "__main__":
    main()
