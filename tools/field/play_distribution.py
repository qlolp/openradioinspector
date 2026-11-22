"""Collect Google's universal Field APK and publish the accepted bytes in an independent release."""
import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen

from tools.field.android_release import (PACKAGE, PLAY_URL, fingerprint, signing_pins, validate_receipt,
                                         verify_apk, version)
from tools.release.macos_rc_package import digest, read_json, write_json
from tools.release.publish_release import (Repository, collect_dispatch_file, compare_remote, github,
                                          verify_github_attestation, verify_remote_assets)
from tools.release.release_artifacts import require, sha, verify_sums, write_sums

BUILDER = "android-field-bundle.yml"
TESTS = "android-field.yml"
WORKFLOW_JOBS = {BUILDER: {"upload-bundle"}, TESTS: {"field", "macos-field-reader"}}
AAB = "field-upload.aab"
RECORD = "android-field-release.json"
RECEIPT = "android-field-acceptance.json"
PROOF = "android-field-acceptance.sigstore.json"


def universal_id(response, pin):
    groups = response.get("generatedApks", [])
    # Multiple serving keys across Android OS versions cannot meet the single-identity contract.
    require(len(groups) == 1, "unexpected Play app-signing certificate set")
    raw = groups[0].get("certificateSha256Hash")
    try:
        actual = fingerprint(raw)
    except ValueError:
        # API clients can present a binary digest as base64; accept only the same 32 bytes.
        require(isinstance(raw, str), "missing Play certificate digest")
        try:
            actual = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True).hex()
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid Play certificate digest") from exc
    require(actual == pin, "unexpected Play app-signing certificate")
    require(not (groups[0].get("unprotectedGeneratedSplitApks") or groups[0].get("unprotectedGeneratedStandaloneApks")),
            "disable Play automatic protection for offline installation")
    download_id = groups[0].get("generatedUniversalApk", {}).get("downloadId")
    require(isinstance(download_id, str) and download_id and download_id not in (".", ".."),
            "Play has not supplied a signed universal APK")
    return quote(download_id, safe="")


def check_inventory(bundles, tracks, code, aab_sha, public):
    require([b.get("sha256") for b in bundles.get("bundles", []) if b.get("versionCode") == code] == [aab_sha],
            "Google Play's uploaded AAB SHA-256 differs from the protected build")
    served = [r for track in tracks.get("tracks", []) if not public or track.get("track") == "production"
              for r in track.get("releases", []) if r.get("status") in {"inProgress", "completed"}]
    require(any(str(code) in r["versionCodes"] and (not public or r["status"] == "completed") for r in served),
            "candidate is not served on the required Play track")
    if public:
        require(max(int(v) for r in served for v in r["versionCodes"]) == code,
                "a higher production versionCode is already served")


class Play:
    def __init__(self):
        self.base = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/" + PACKAGE + "/"
        self.token = os.environ["FIELD_PLAY_TOKEN"]

    def request(self, path, method="GET", destination=None):
        req = Request(self.base + path, method=method, data=b"{}" if method == "POST" else None,
                      headers={"Content-Type": "application/json"})
        # Google may redirect the APK to a signed media URL. Keep the bearer token on the API origin.
        req.add_unredirected_header("Authorization", "Bearer " + self.token)
        with urlopen(req, timeout=90) as response:
            if destination is not None:
                with destination.open("xb") as output:
                    shutil.copyfileobj(response, output)
            elif method != "DELETE":
                return json.load(response)

    def check(self, code, aab_sha, public):
        # Play exposes bundle digests and tracks inside a temporary edit. No change is ever committed.
        edit = "edits/" + quote(self.request("edits", method="POST")["id"], safe="")
        try:
            check_inventory(self.request(edit + "/bundles"), self.request(edit + "/tracks"), code, aab_sha, public)
        finally:
            self.request(edit, method="DELETE")

    def download(self, code, pin, destination):
        path = f"generatedApks/{code}"
        ident = universal_id(self.request(path), pin)
        self.request(f"{path}/downloads/{ident}:download?alt=media", destination=destination)


def check_run(run, jobs, repo, workflow):
    require(run.get("path") == ".github/workflows/" + workflow and run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == "main" and run.get("head_repository", {}).get("full_name") == repo
            and sha(run.get("head_sha"), 40) and run.get("status") == "completed" and run.get("conclusion") == "success",
            "expected a successful dispatch on protected main in this repository")
    require(jobs["total_count"] == len(jobs["jobs"])
            and WORKFLOW_JOBS[workflow] == {j["name"] for j in jobs["jobs"]}
            and all(j.get("conclusion") == "success" for j in jobs["jobs"]), "incomplete Field workflow jobs")


def completed_run(repo, run_id, workflow):
    require(run_id > 0, "invalid workflow run ID")
    run = repo.api(f"actions/runs/{run_id}")
    jobs = repo.api(f"actions/runs/{run_id}/attempts/{int(run['run_attempt'])}/jobs?per_page=100")
    check_run(run, jobs, repo.repo, workflow)
    subprocess.run(["git", "merge-base", "--is-ancestor", run["head_sha"], "refs/remotes/origin/main"], check=True)
    return run


def origin(repo, build_id, test_id):
    build = completed_run(repo, build_id, BUILDER)
    tests = completed_run(repo, test_id, TESTS)
    revision = build["head_sha"]
    require(tests["head_sha"] == revision, "Android emulator and Mac reader must pass on the signed revision")
    props = subprocess.check_output(["git", "show", revision + ":clients/android/gradle.properties"], text=True)
    name, code = version(props, build["run_number"])
    return dict(source_revision=revision, review_revision=repo.revision, version_name=name, version_code=code, build_run_id=build_id,
                build_attempt=build["run_attempt"], test_run_id=test_id, test_attempt=tests["run_attempt"])


def apk_name(build):
    return f"OpenRadioInspector-Field-{build['version_name']}-{build['version_code']}-universal.apk"


def review(build, candidate, out, play):
    verify_sums(candidate, {AAB})
    out.mkdir()
    build["aab_sha256"] = digest(candidate / AAB)
    pin, upload = signing_pins()
    play.check(build["version_code"], build["aab_sha256"], public=False)
    apk = out / apk_name(build)
    play.download(build["version_code"], pin, apk)
    verify_apk(apk, build["version_name"], build["version_code"], pin)
    collect_dispatch_file(out, RECEIPT, "FIELD_RECEIPT_B64", 16 * 1024)
    receipt = read_json(out / RECEIPT)
    build["apk_sha256"] = digest(apk)
    validate_receipt(receipt, build, build["apk_sha256"])
    with tempfile.TemporaryDirectory() as scratch:
        prior = Path(scratch) / "previous.apk"
        play.download(receipt["previous_code"], pin, prior)
        verify_apk(prior, None, receipt["previous_code"], pin)
        require(digest(prior) == receipt["previous_apk_sha256"], "wrong previous Play universal APK")
    write_json(out / RECORD, dict(schema_version=1, application_id=PACKAGE, play_url=PLAY_URL,
                                  **build, apk=apk.name, apk_bytes=apk.stat().st_size,
                                  play_cert_sha256=pin, upload_cert_sha256=upload,
                                  acceptance_sha256=digest(out / RECEIPT)))
    write_sums(out)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write("## Android Field review\n\nCompare these hashes to the private S22 reports. "
                      "Roll out this exact bundle in Play and verify the public listing before approving publication.\n\n")
        for path in (out / RECORD, out / RECEIPT):
            summary.write("```json\n" + json.dumps(read_json(path), indent=2) + "\n```\n")


def approved(repo):
    reviews = repo.api(f"actions/runs/{int(os.environ['GITHUB_RUN_ID'])}/approvals")
    require(any(r.get("state") == "approved" and any(e["name"] == "android-field-publication" for e in r["environments"])
                for r in reviews), "a recorded Field publication environment review is required")


def reviewed_files(repo, out, expected_sums):
    require(sha(expected_sums) and digest(out / "SHA256SUMS") == expected_sums, "reviewed checksums changed")
    record = read_json(out / RECORD)
    require(sha(record.get("review_revision"), 40) and record["review_revision"] == repo.revision,
            "review and publication workflow revisions differ")
    apk = apk_name(record)
    verify_sums(out, {apk, RECORD, RECEIPT})
    require(record["application_id"] == PACKAGE and digest(out / apk) == record["apk_sha256"]
            and record["apk"] == apk and (out / apk).stat().st_size == record["apk_bytes"]
            and record["acceptance_sha256"] == digest(out / RECEIPT), "reviewed application, APK or receipt changed")
    receipt = read_json(out / RECEIPT)
    validate_receipt(receipt, record, record["apk_sha256"])
    approved(repo)
    return record, receipt


def seal_acceptance(repo, out, proof, existing_assets):
    # A rerun issues a fresh Sigstore bundle for the same bytes. Resume using
    # the first valid proof already uploaded to this release.
    destination = out / PROOF
    previous = [asset for asset in existing_assets if asset["name"] == PROOF]
    require(len(previous) <= 1, "ambiguous acceptance attestation")
    if previous:
        asset = previous[0]
        require(0 < asset["size"] <= 128 * 1024, "invalid remote Field attestation size")
        with destination.open("xb") as stream:
            github("-H", "Accept: application/octet-stream", repo.base + f"releases/assets/{asset['id']}", output=stream)
        compare_remote(asset, destination)
    else:
        require(proof is not None and proof.is_file() and not proof.is_symlink() and 0 < proof.stat().st_size <= 128 * 1024,
                "a GitHub acceptance attestation is required")
        shutil.copyfile(proof, destination)
    verify_github_attestation(repo, out / RECEIPT, destination, workflow="android-field-release.yml", branches=("main",))
    write_sums(out)


def prior_public_code(releases, tag):
    codes = []
    for release in releases:
        other = release["tag_name"]
        if release["draft"] or other == tag or not other.startswith("android-field-v"):
            continue
        pattern = r"OpenRadioInspector-Field-" + re.escape(other.removeprefix("android-field-v")) + r"-(\d+)-universal\.apk"
        matches = [m for a in release["assets"] if (m := re.fullmatch(pattern, a["name"]))]
        require(release.get("immutable") is True and len(matches) == 1, "unexpected earlier Android release")
        codes.append(int(matches[0].group(1)))
    return max(codes, default=0)


def publish(repo, out, expected_sums, play, proof=None):
    record, receipt = reviewed_files(repo, out, expected_sums)
    pin, upload = signing_pins()
    require(record["play_cert_sha256"] == pin and record["upload_cert_sha256"] == upload, "reviewed certificate pins differ")
    play.check(record["version_code"], record["aab_sha256"], public=True)
    repo.immutable()
    tag = "android-field-v" + record["version_name"]
    revision = record["source_revision"]
    body = (f"OpenRadioInspector Field {record['version_name']} (versionCode {record['version_code']})\n\n"
            f"{PLAY_URL}\n\nAndroid 12+, ARM64. Play app-signing certificate (SHA-256) `{pin}`.\n\n"
            f"Source `{revision}`. Reviewer-approved asset list SHA-256 `{expected_sums}`. "
            f"Acceptance workflow commit `{repo.revision}`.")
    fields = dict(tag_name=tag, name=tag, body=body, target_commitish=revision, prerelease=False)
    pages = github("--paginate", "--slurp", repo.base + "releases?per_page=100")
    releases = [release for page in pages for release in page]
    existing = [r for r in releases if r["tag_name"] == tag]
    require(len(existing) <= 1, "ambiguous Field tag")
    if existing:
        release = existing[0]
        require(all(release.get(k) == v for k, v in fields.items()), "tag already belongs to a different release")
    else:
        require(repo.tag_ref(tag, optional=True) is None, "tag already exists without a matching Field release")
    seal_acceptance(repo, out, proof, existing[0]["assets"] if existing else [])
    if not existing or release["draft"]:
        previous = prior_public_code(releases, tag)
        require(previous == 0 or previous == receipt["previous_code"],
                "upgrades must start from the previous public Field release")
        if not existing:
            release = github("-X", "POST", repo.base + "releases", data={**fields, "draft": True})
    paths = {path.name: path for path in out.iterdir()}
    endpoint = f"releases/{release['id']}"
    if release["draft"]:
        ref = repo.tag_ref(tag, optional=True)
        require(ref is None or ref["object"]["sha"] == revision, "Field tag already points elsewhere")
        present = set()
        for asset in release["assets"]:
            require(asset["name"] in paths and asset["name"] not in present, "unexpected release draft asset")
            compare_remote(asset, paths[asset["name"]])
            present.add(asset["name"])
        # The final checksums include the nondeterministic proof. Upload the
        # proof first, so every interrupted upload can reconstruct the same list.
        for name in sorted(paths.keys() - present, key=lambda name: (name != PROOF, name)):
            subprocess.run(["gh", "release", "upload", tag, str(paths[name]), "--repo", repo.repo], check=True)
        verify_remote_assets(repo.api(endpoint), paths)
        repo.immutable()
        release = github("-X", "PATCH", repo.base + endpoint, data={"draft": False, "make_latest": "false"})
    require(release.get("immutable") is True and not release["draft"]
            and repo.tag_ref(tag)["object"]["sha"] == revision, "public Field release or tag identity unconfirmed")
    verify_remote_assets(repo.api(endpoint), paths)
    print(release["html_url"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("origin", "review", "approve", "publish"))
    parser.add_argument("--build-run", type=int)
    parser.add_argument("--test-run", type=int)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--proof", type=Path)
    args = parser.parse_args()
    repo = Repository()
    require(os.environ["GITHUB_REF"] == "refs/heads/main", "protected main required")
    if args.command == "approve":
        reviewed_files(repo, args.out, os.environ["FIELD_REVIEWED_SUMS"])
        return
    if args.command == "publish":
        publish(repo, args.out, os.environ["FIELD_REVIEWED_SUMS"], Play(), args.proof)
        return
    build = origin(repo, args.build_run, args.test_run)
    artifact = f"android-field-upload-{args.build_run}-{build['build_attempt']}"
    if args.command == "origin":
        print("artifact=" + artifact)
    else:
        require(os.environ["FIELD_CANDIDATE_ARTIFACT"] == artifact, "candidate workflow attempt changed")
        review(build, args.candidate, args.out, Play())


if __name__ == "__main__":
    main()
