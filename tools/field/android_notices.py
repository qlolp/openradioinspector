"""Stage and verify the notices for the locked, shipped Android Field runtime."""
import argparse
from collections import defaultdict
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from zipfile import ZipFile

from tools.release import native_notices as native
from tools.release.macos_rc_package import digest, read_json
from tools.release.release_artifacts import require, sha

ROOT = native.ROOT
SOURCES = ROOT / "clients/android/notices"
TARGET = "aarch64-linux-android"
FIELD = "ori-android-field"
USB = "com.github.mik3y:usb-serial-for-android:3.9.0"
INDEX = "licenses.json"
# These registry archives omit the licenses at their upstream repository root.
JNI = {
    "jni:0.22.4": ("5ae9458a4ec44c5318f37ddc7569c1d4ae8a69e7", ("jni-rs-MIT.txt", "jni-rs-APACHE.txt")),
    "jni-macros:0.22.4": ("33045a124105c939d1e2cbdcb5a39e5d868ffa03", ("jni-rs-MIT.txt", "jni-rs-APACHE.txt")),
    "jni-sys-macros:0.4.1": ("64d77b7a5f119d7b55b4e2c169a4668067ff59e6", ("jni-sys-MIT.txt", "Apache-2.0.txt")),
}
# Include extra terms and attribution absent from the runtime AARs/JARs.
MAVEN = {
    USB: ("MIT AND Apache-2.0", ("usb-serial-MIT.txt", "usb-serial-attributions.txt")),
    "org.jetbrains.kotlin:kotlin-stdlib:2.1.21": (
        "Apache-2.0 AND BSD-3-Clause AND BSL-1.0", ("kotlin-COPYRIGHT.txt", "kotlin-gwt_license.txt",
        "kotlin-guava_license.txt", "kotlin-threetenbp_license.txt", "kotlin-boost_LICENSE.txt", "kotlin-stdlib-sources.txt")),
    "org.jetbrains.kotlinx:kotlinx-coroutines-core-jvm:1.10.2": ("Apache-2.0", ("kotlin-coroutines-NOTICE.txt",)),
    "org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2": ("Apache-2.0", ("kotlin-coroutines-NOTICE.txt",)),
    "org.jetbrains.kotlinx:kotlinx-serialization-core-jvm:1.7.3": ("Apache-2.0", ("kotlin-serialization-NOTICE.txt",)),
    "org.jetbrains:annotations:23.0.0": ("Apache-2.0", ()),
    "org.jspecify:jspecify:1.0.0": ("Apache-2.0", ()),
    "com.google.guava:listenablefuture:1.0": ("Apache-2.0", ()),
}


def put_text(output, data):
    require(data, "empty notice text")
    key = hashlib.sha256(data).hexdigest()
    # A shared license is distributed once. The inventory retains its name and origin for every component.
    path = output / "text" / (key + ".txt")
    if not path.exists():
        path.write_bytes(data)
    return key


def supplement(output, names, origins):
    result = {}
    for name in names:
        row = origins[name]
        data = (SOURCES / "upstream" / row.get("file", name)).read_bytes()
        key = put_text(output, data)
        require(key == row["sha256"], f"upstream notice differs: {name}")
        result[name] = key
    return result


def cargo_components(output, origins, offline):
    metadata = native.cargo_metadata(ROOT / "crates" / FIELD / "Cargo.toml", TARGET, ["--offline"] if offline else [])
    raw = subprocess.check_output(["cargo", "tree", "--locked", "--offline", "-p", FIELD, "--target", TARGET,
                                   "--edges", "normal,build", "--prefix", "none", "--format", "{p}"], text=True, cwd=ROOT)
    # Workspace metadata can unify optional features of unrelated binaries. Cargo tree is rooted at this cdylib.
    selected = {tuple(line.split()[:2]) for line in raw.splitlines()}
    packages = [p for p in native.selected_packages(metadata, {FIELD}) if (p["name"], "v" + p["version"]) in selected]
    require(len(packages) == len(selected), "ambiguous or incomplete Field Cargo runtime graph")
    project_key = put_text(output, (ROOT / "LICENSE").read_bytes())
    result = {"OpenRadioInspector": dict(license="MIT", notices={"LICENSE": project_key})}
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for p in packages:
            ident = p["name"] + ":" + p["version"]
            source = Path(p["manifest_path"]).parent
            notices = {"LICENSE": project_key}
            if p["source"] is not None:
                require(p["source"] == "registry+https://github.com/rust-lang/crates.io-index", "unreviewed Cargo source")
                dest = scratch / p["name"] / p["version"]
                paths = native.copy_notices(source, dest, scratch, p["license_file"])
                notices = {(scratch / f).relative_to(dest).as_posix(): put_text(output, (scratch / f).read_bytes()) for f in paths}
                if ident in JNI:
                    revision, supplements = JNI[ident]
                    require(read_json(source / ".cargo_vcs_info.json")["git"]["sha1"] == revision, "JNI source differs")
                    notices.update(supplement(output, supplements, origins))
            require(notices, f"no license text for {ident}")
            result["cargo:" + ident] = dict(license=p["license"] or "MIT", notices=notices)
    return result


def artifact_notices(archive, output, prefix=""):
    result = {}
    for entry in archive.infolist():
        name = entry.filename
        leaf = name.rsplit("/", 1)[-1].upper()
        if not entry.is_dir() and leaf.startswith(("LICENSE", "LICENCE", "NOTICE", "COPYING", "COPYRIGHT", "UNLICENSE")):
            result[prefix + name] = put_text(output, archive.read(entry))
        elif not prefix and (name == "classes.jar" or (name.startswith("libs/") and name.endswith(".jar"))):
            with ZipFile(BytesIO(archive.read(entry))) as nested:
                result.update(artifact_notices(nested, output, name + "!"))
    return result


def reviewed_runtime_files(resolved, pins):
    observations = defaultdict(list)
    for row in resolved:
        path = Path(row["file"])
        observations[row["coordinate"]].append((row, path, digest(path)))
    missing = sorted(pins.keys() - observations.keys())
    errors = ["Missing " + ", ".join(missing)] if missing else []
    files = {}
    for coord, observed in sorted(observations.items()):
        expected = pins.get(coord)
        if expected is None or any(actual != expected for _, _, actual in observed):
            details = "\n".join(f"  {row['artifact']} [{row['variant']}] file={path.name} sha256={actual}"
                                for row, path, actual in observed)
            errors.append(f"{coord} expected sha256={expected or 'unreviewed'}\n{details}")
        else:
            # Gradle may return the same pinned AAR through several selected variants.
            files[coord] = observed[0][1]
    require(not errors, "Android releaseRuntimeClasspath artifact changed:\n" + "\n".join(errors))
    return files


def maven_components(output, resolved, pins, origins):
    components = {}
    for coord, path in reviewed_runtime_files(resolved, pins).items():
        license_id, extra = ("Apache-2.0", ()) if coord.startswith("androidx.") else MAVEN[coord]
        with ZipFile(path) as archive:
            files = artifact_notices(archive, output)
        files.update(supplement(output, ("Apache-2.0.txt", *extra), origins))
        components["maven:" + coord] = dict(artifact_sha256=pins[coord], license=license_id, notices=files)
    return components


def stage(output, resolved, offline):
    pins, origins = read_json(SOURCES / "runtime-sha256.json"), read_json(SOURCES / "origins.json")
    output.mkdir(parents=True)
    (output / "text").mkdir()
    components = maven_components(output, resolved, pins, origins)
    components.update(cargo_components(output, origins, offline))
    sysroot = Path(subprocess.check_output(["rustc", "--print", "sysroot"], text=True, cwd=ROOT).strip())
    rust = subprocess.check_output(["rustc", "--version"], text=True, cwd=ROOT).strip()
    std = (sysroot / "share/doc/rust/COPYRIGHT-library.html").read_bytes()
    components["rust:std"] = dict(toolchain=rust, notices={"COPYRIGHT-library.html": put_text(output, std)})
    native.write_json(output / INDEX, dict(schema_version=1, target=TARGET, components=components,
                                            upstream_notices=origins))
    return digest(output / INDEX)


def verify_package(path, expected=None):
    prefix = ("base/" if path.suffix == ".aab" else "") + "assets/notices/"
    with ZipFile(path) as archive:
        items = [n for n in archive.namelist() if n.startswith(prefix) and not n.endswith("/")]
        require(len(items) == len(set(items)) and prefix + INDEX in items, "missing or duplicate Field notice inventory")
        data = archive.read(prefix + INDEX)
        index_hash = hashlib.sha256(data).hexdigest()
        require(expected is None or index_hash == expected, "notice inventory differs from the reviewed source or upload bundle")
        index = json.loads(data)
        components = index["components"]
        require(index.get("schema_version") == 1 and index.get("target") == TARGET
                and {"OpenRadioInspector", "rust:std", "maven:" + USB} <= components.keys()
                and any(k.startswith("cargo:tokio:") for k in components)
                and any(k.startswith("cargo:" + FIELD + ":") for k in components), "incomplete Field notice components")
        hashes = set()
        for key, row in components.items():
            entries = row["notices"]
            require(isinstance(entries, dict) and entries and all(sha(v) for v in entries.values()), f"missing notices: {key}")
            hashes.update(entries.values())
        require(set(items) == {prefix + INDEX, *(prefix + "text/" + h + ".txt" for h in hashes)},
                "missing or unexpected bundled license text")
        for h in hashes:
            require(hashlib.sha256(archive.read(prefix + "text/" + h + ".txt")).hexdigest() == h,
                    "bundled notice text hash differs")
    return index_hash


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--resolved", type=Path, required=True)
    collect.add_argument("--assets", type=Path, required=True)
    collect.add_argument("--offline", action="store_true")
    collect.add_argument("--review", action="store_true", help="write a new directory for local notice review")
    verify = sub.add_parser("verify")
    verify.add_argument("package", type=Path)
    args = p.parse_args()
    pinned = (SOURCES / "inventory.sha256").read_text().strip()
    if args.command == "verify":
        print(verify_package(args.package, pinned))
        return
    if args.review:
        require(not args.assets.exists(), "use a new directory for a proposed inventory")
    args.assets.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.assets.parent) as temp:
        new = Path(temp) / "notices"
        actual = stage(new, read_json(args.resolved), args.offline)
        require(args.review or actual == pinned, "review the changed Android notices inventory")
        args.assets.mkdir(exist_ok=True)
        target = args.assets / "notices"
        if target.exists():
            shutil.rmtree(target)
        new.rename(target)
        print("Field notice inventory SHA-256", actual)


if __name__ == "__main__":
    main()
