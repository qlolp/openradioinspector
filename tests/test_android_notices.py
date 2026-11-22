"""Check missing/falsified Field notices without an Android, Java or Rust toolchain."""
import hashlib
import json
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from tools.field import android_notices as notices


class AndroidNoticesTests(unittest.TestCase):
    def test_source_attribution_includes_usb_header_and_locked_runtime(self):
        sources = notices.SOURCES
        origins = notices.read_json(sources / "origins.json")
        for name, row in origins.items():
            self.assertEqual(notices.digest(sources / "upstream" / row.get("file", name)), row["sha256"])
            self.assertTrue(row["url"].startswith("https://"))
        self.assertIn(notices.USB, notices.read_json(sources / "runtime-sha256.json"))
        driver = (sources / "upstream/usb-serial-attributions.txt").read_text()
        self.assertIn("HexDump.java", driver)
        self.assertIn("Apache License, Version 2.0", driver)
        self.assertIn("Copyright 2014 Andreas Butti", driver)

    def test_aab_and_apk_require_the_same_actual_notice_text_and_inventory(self):
        data = {component: component.encode() for component in ("OpenRadioInspector", "rust:std", "cargo:tokio:1.0",
                                                                  "cargo:ori-android-field:0.1.0", "maven:" + notices.USB)}
        components = {key: {"notices": {"LICENSE": hashlib.sha256(value).hexdigest()}} for key, value in data.items()}
        index = json.dumps(dict(schema_version=1, target=notices.TARGET, components=components)).encode()
        files = {"licenses.json": index}
        files.update({"text/" + hashlib.sha256(v).hexdigest() + ".txt": v for v in data.values()})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def write(ext, members):
                path = root / ("candidate." + ext)
                prefix = ("base/" if ext == "aab" else "") + "assets/notices/"
                with ZipFile(path, "w") as archive:
                    for name, content in members.items():
                        archive.writestr(prefix + name, content)
                return path
            expected = notices.verify_package(write("aab", files))
            self.assertEqual(notices.verify_package(write("apk", files), expected), expected)
            tokio = "text/" + components["cargo:tokio:1.0"]["notices"]["LICENSE"] + ".txt"
            for bad in [{k: v for k, v in files.items() if k != tokio}, {**files, tokio: b"tampered"},
                        {**files, "extra": b"unexpected"}, {}]:
                with self.assertRaises(ValueError):
                    notices.verify_package(write("apk", bad), expected)
            altered = json.loads(index)
            del altered["components"]["cargo:tokio:1.0"]
            bad = {k: v for k, v in files.items() if k != tokio}
            bad["licenses.json"] = json.dumps(altered).encode()
            with self.assertRaises(ValueError):
                notices.verify_package(write("apk", bad), expected)
            with self.assertRaises(ValueError):
                notices.verify_package(write("aab", bad))

    def test_runtime_jar_notices_survive_android_packaging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "text").mkdir()
            nested = BytesIO()
            with ZipFile(nested, "w") as jar:
                jar.writestr("META-INF/NOTICE.txt", b"upstream notice")
            outer = BytesIO()
            with ZipFile(outer, "w") as aar:
                aar.writestr("META-INF/LICENSE.txt", b"upstream license")
                aar.writestr("classes.jar", nested.getvalue())
                aar.writestr("lib/anything", b"irrelevant")
            with ZipFile(outer) as aar:
                copies = notices.artifact_notices(aar, root)
            self.assertEqual(set(copies), {"classes.jar!META-INF/NOTICE.txt", "META-INF/LICENSE.txt"})
            self.assertEqual({p.read_bytes() for p in (root / "text").iterdir()}, {b"upstream license", b"upstream notice"})

    def test_maven_inputs_match_all_reviewed_artifact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aar = root / "usb.aar"
            (root / "text").mkdir()
            with ZipFile(aar, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"fixture")
            pins = {notices.USB: notices.digest(aar)}
            origins = notices.read_json(notices.SOURCES / "origins.json")
            row = dict(coordinate=notices.USB, artifact="usb.aar", variant="release", file=str(aar))
            actual = notices.maven_components(root, [row], pins, origins)["maven:" + notices.USB]
            self.assertEqual(actual["license"], "MIT AND Apache-2.0")
            self.assertEqual(set(actual["notices"]), {"usb-serial-MIT.txt", "usb-serial-attributions.txt", "Apache-2.0.txt"})
            for inputs in ([], [row, dict(row, coordinate="other")]):
                with self.assertRaisesRegex(ValueError, "Classpath"):
                    notices.maven_components(root, inputs, pins, origins)
            aar.write_bytes(b"unexpected dependency bytes")
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                notices.maven_components(root, [row], pins, origins)

    def test_gradle_release_and_adhoc_variants_must_each_match_the_reviewed_aar(self):
        coord = "androidx.core:core:1.13.1"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "text").mkdir()
            release = root / "core-1.13.1.aar"
            with ZipFile(release, "w") as archive:
                archive.writestr("LICENSE", b"AndroidX license")
            repeated = root / "other-cache" / release.name
            repeated.parent.mkdir()
            repeated.write_bytes(release.read_bytes())
            primary = dict(coordinate=coord, artifact="core-1.13.1.aar", variant="releaseVariantReleaseRuntimePublication",
                           file=str(release))
            adhoc = dict(primary, variant="adhoc", file=str(repeated))
            pins = {coord: notices.digest(release)}
            origins = notices.read_json(notices.SOURCES / "origins.json")
            components = notices.maven_components(root, [primary, adhoc], pins, origins)
            self.assertEqual(list(components), ["maven:" + coord])
            self.assertEqual(components["maven:" + coord]["artifact_sha256"], pins[coord])

            repeated.write_bytes(b"unreviewed classified dependency")
            alternate = dict(adhoc, artifact="core-1.13.1-alternate.aar")
            for rows in ([primary, alternate], [alternate, primary]):
                with self.assertRaises(ValueError) as fault:
                    notices.maven_components(root, rows, pins, origins)
                for detail in (coord, primary["variant"], alternate["variant"], alternate["artifact"],
                               release.name, pins[coord], notices.digest(repeated)):
                    self.assertIn(detail, str(fault.exception))
