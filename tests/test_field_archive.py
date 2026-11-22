"""Offline interoperability and interrupted/corrupt field export tests."""
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import tempfile
import unittest

from tools import field_archive as archive

FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
EXAMPLE = FIXTURES / "field-archive-v1.orifield"


def line(obj):
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def fixture_contents():
    source = json.loads((FIXTURES / "measurement-v1.json").read_bytes())
    session = source["session"]["id"]
    streams = source["session"]["streams"]
    start, end = "2026-11-11T12:00:00.123456Z", "2026-11-11T12:00:00.223456Z"
    sweep, observation = source["sweep"], source["observation"]
    sweep["interval"] = {"started_at": start, "ended_at": end}
    sweep["settings"]["field_fix_id"] = "fix-1"
    header = {"format": "openradioinspector-archive-ndjson", "format_version": 1, "session_id": session}
    base = {"session_id": session, "stream_id": sweep["stream_id"], "time_utc": start}
    gps = dict(base, kind="gps", id="fix-1", longitude=0.125, latitude=-0.25, accuracy_m=5.5)
    note = dict(base, kind="note", id="note-1", text="Synthetic marker", sweep_id=sweep["id"])
    gap = dict(base, kind="gap", id="gap-1", stream_id=observation["stream_id"],
               time_utc=end, reason="Synthetic receiver reconnect", observation_id=observation["id"])
    feature = {"type": "Feature", "id": "fix-1", "geometry": {"type": "Point", "coordinates": [0.125, -0.25]},
               "properties": dict(base, accuracy_m=5.5)}
    payload = {
        archive.MEASUREMENTS: line(header) + line({"kind": "sweep", "measurement": sweep})
        + line({"kind": "observation", "measurement": observation}),
        archive.EVENTS: line(gps) + line(note) + line(gap),
        archive.TRACK: archive.TRACK_START + line(feature) + b"]}\n",
    }
    manifest = {"format": "openradioinspector-field", "format_version": 1,
                "session_id": session, "stream_ids": [s["id"] for s in streams],
                "streams": [dict(id=s["id"], device=s["device"], profile="synthetic",
                                 settings={}) for s in streams],
                "sweep_count": 1, "observation_count": 1,
                "files": {k: {"records": n} for k, n in zip(payload, (2, 3, 1))}}
    return manifest, payload


def pack(target, manifest, payload):
    for name, data in payload.items():
        manifest["files"][name].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    with zipfile.ZipFile(target, "w") as z:
        for name, data in {**payload, archive.MANIFEST: line(manifest)}.items():
            entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            entry.create_system = 0
            entry.external_attr = 0x20
            z.writestr(entry, data)


class FieldArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_shared_example_and_lossless_extract(self):
        tmp_path = self.root
        manifest, payload = fixture_contents()
        reproduction = tmp_path / "example.orifield"
        pack(reproduction, manifest, payload)
        assert reproduction.read_bytes() == EXAMPLE.read_bytes()
        out = tmp_path / "contents"
        assert archive.validate(EXAMPLE, out) == manifest
        assert {p.name for p in out.iterdir()} == {*payload, archive.MANIFEST}
        for name, raw in payload.items():
            assert (out / name).read_bytes() == raw
        with self.assertRaisesRegex(ValueError, "destination already exists"):
            archive.validate(EXAMPLE, out)


    def test_corrupt_archive_never_publishes(self):
        for problem in ("truncated", "digest", "count", "scope", "dangling", "duplicate",
                        "coordinates", "feature", "extra", "version", "boolean", "newline", "track_boolean"):
            with self.subTest(problem=problem):
                self.check_corrupt(problem)


    def check_corrupt(self, problem):
        tmp_path = self.root
        manifest, payload = fixture_contents()
        m, e, t = archive.MEASUREMENTS, archive.EVENTS, archive.TRACK
        if problem == "count":
            manifest["files"][t]["records"] += 1
        elif problem == "scope":
            payload[e] = payload[e].replace(b'"stream_id":"spectrum/one"', b'"stream_id":"alien"')
        elif problem == "dangling":
            payload[m] = payload[m].replace(b'"field_fix_id":"fix-1"', b'"field_fix_id":"missing"')
        elif problem == "duplicate":
            payload[e] += payload[e].splitlines(keepends=True)[1]
            manifest["files"][e]["records"] += 1
        elif problem == "coordinates":
            payload[e] = payload[e].replace(b'"latitude":-0.25', b'"latitude":91')
        elif problem == "track_boolean":
            payload[e] = payload[e].replace(b'"accuracy_m":5.5', b'"accuracy_m":1')
            payload[t] = payload[t].replace(b'"accuracy_m":5.5', b'"accuracy_m":true')
        elif problem == "feature":
            payload[t] = payload[t].replace(b"[0.125,-0.25]", b"[-0.25,0.125]")
        elif problem == "version":
            manifest["format_version"] = 2
        elif problem == "boolean":
            manifest["sweep_count"] = True
        elif problem == "newline":
            payload[m] = payload[m].rstrip(b"\n")
        path = tmp_path / "bad.orifield"
        pack(path, manifest, payload)
        if problem == "extra":
            with zipfile.ZipFile(path, "a") as z:
                z.writestr("../private", "bad")
        elif problem == "truncated":
            path.write_bytes(path.read_bytes()[:-10])
        elif problem == "digest":
            # Recompute ZIP CRC, leaving manifest SHA-256 unchanged.
            with zipfile.ZipFile(path) as z:
                members = {n: z.read(n) for n in z.namelist()}
            members[e] = members[e].replace(b"marker", b"reader")
            with zipfile.ZipFile(path, "w") as z:
                for n, data in members.items():
                    z.writestr(n, data)
        destination = tmp_path / "contents"
        with self.assertRaises((ValueError, sqlite3.IntegrityError)):
            archive.validate(path, destination)
        assert not destination.exists()
        assert not (tmp_path / "private").exists()


    def test_full_receiver_list_fits_stream_descriptor(self):
        manifest, payload = fixture_contents()
        descriptor = manifest["streams"][1]
        descriptor["settings"] = {"kind": "ardv1_list", "frequencies_hz": list(range(100000000, 100010000))}
        self.assertGreater(len(line(manifest)), 65536)
        path = self.root / "full-list.orifield"
        pack(path, manifest, payload)
        self.assertEqual(archive.validate(path)["streams"][1], descriptor)

    def test_android_nanosecond_intervals(self):
        self.assertLess(archive.utc("2026-11-22T21:30:00.123456781Z"),
                        archive.utc("2026-11-23T00:30:00.123456782+03:00"))
        self.assertEqual(archive.utc("2026-11-22T21:30:00.1Z"),
                         archive.utc("2026-11-22T21:30:00.100000000+00:00"))

    def test_empty_capture_with_no_location(self):
        tmp_path = self.root
        manifest, payload = fixture_contents()
        payload[archive.MEASUREMENTS] = payload[archive.MEASUREMENTS].splitlines(keepends=True)[0]
        payload[archive.EVENTS] = b""
        payload[archive.TRACK] = archive.TRACK_START + b"]}\n"
        manifest.update(sweep_count=0, observation_count=0)
        for entry in manifest["files"].values():
            entry["records"] = 0
        path = tmp_path / "empty.orifield"
        pack(path, manifest, payload)
        assert archive.validate(path)["sweep_count"] == 0
