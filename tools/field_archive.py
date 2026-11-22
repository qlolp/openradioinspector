"""Validate and optionally extract a V1 .orifield package offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import struct
import sys
import tempfile
import zipfile
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path

MEASUREMENTS, EVENTS, TRACK = PAYLOADS = ("measurements.ndjson", "events.ndjson", "track.geojson")
MANIFEST = "manifest.json"
MAX_LINE = 16 * 1024 * 1024 + 1024
MAX_MANIFEST = 256 * 1024
TRACK_START = b'{"type":"FeatureCollection","features":[\n'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def obj(value, label):
    require(isinstance(value, dict), f"invalid {label}")
    return value


def string(value, label):
    require(isinstance(value, str) and bool(value.strip()), f"invalid {label}")
    return value


def number(value, label):
    require(type(value) in (int, float) and math.isfinite(value), f"invalid {label}")
    return value


def count(value, label):
    require(type(value) is int and value >= 0, f"invalid {label}")
    return value


def utc(value):
    string(value, "timestamp")
    match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
                         r"(?:\.([0-9]+))?(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value)
    require(match, "invalid timestamp")
    whole, fraction, offset = match.groups()
    # Validate the calendar and retain sub-microsecond ordering on Python 3.10+.
    return datetime.fromisoformat(whole + offset.replace("Z", "+00:00")), (fraction or "").rstrip("0")


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key {key}")
        result[key] = value
    return result


def parse(data):
    def finite(token):
        return number(float(token), "JSON number")

    return obj(json.loads(data.decode("utf-8"), object_pairs_hook=unique_fields,
                          parse_float=finite, parse_constant=finite), "JSON object")


def open_zip(source):
    # zipfile reads the central directory into memory. Bound its size before opening it.
    source.seek(0, 2)
    size = source.tell()
    require(22 <= size <= 0xfffffffe, "invalid classic ZIP size")
    source.seek(-22, 2)
    end = struct.unpack("<4s4H2LH", source.read(22))
    require(end[:5] == (b"PK\x05\x06", 0, 0, 4, 4) and end[-1] == 0
            and end[5] <= 65536 and end[5] + end[6] + 22 == size,
            "expected four entries in a classic field ZIP")
    require(size >= 42, "truncated ZIP")
    source.seek(-42, 2)
    require(source.read(4) != b"PK\x06\x07", "ZIP64 locator outside classic field format")
    archive = zipfile.ZipFile(source)
    try:
        names = archive.namelist()
        require(len(names) == 4 and set(names) == {*PAYLOADS, MANIFEST}
                and names[-1] == MANIFEST, "unexpected field archive entries or manifest order")
        ranges = []
        for info in archive.infolist():
            require(info.compress_type == zipfile.ZIP_STORED and info.compress_size == info.file_size
                    and info.flag_bits & ~0x800 == 0 and info.volume == 0
                    and (info.external_attr >> 16) & 0o170000 in (0, 0o100000)
                    and not info.external_attr & 0x10, f"unsupported ZIP encoding for {info.filename}")
            source.seek(info.header_offset)
            header = source.read(30)
            require(len(header) == 30, "truncated local ZIP header")
            fields = struct.unpack("<4s5H3L2H", header)
            require(fields[0] == b"PK\x03\x04" and fields[2:4] == (info.flag_bits, zipfile.ZIP_STORED)
                    and fields[6:9] == (info.CRC, info.file_size, info.file_size),
                    f"inconsistent local ZIP header for {info.filename}")
            require(source.read(fields[9]) == info.filename.encode("ascii"), "inconsistent ZIP member name")
            data_end = source.tell() + fields[10] + info.file_size
            ranges.append((info.header_offset, data_end, info.filename))
        ordered = sorted(ranges)
        require(ordered[0][0] == 0 and ordered[-1][1:] == (end[6], MANIFEST)
                and all(a[1] == b[0] for a, b in zip(ordered, ordered[1:])),
                "invalid field archive boundaries")
        return archive
    except BaseException:
        archive.close()
        raise


def read_manifest(archive):
    require(archive.getinfo(MANIFEST).file_size <= MAX_MANIFEST, "manifest exceeds limit")
    raw = archive.read(MANIFEST)
    m = parse(raw)
    require(m.get("format") == "openradioinspector-field"
            and count(m.get("format_version"), "format_version") == 1, "unsupported field format")
    string(m.get("session_id"), "session_id")
    ids = m.get("stream_ids")
    require(isinstance(ids, list) and 1 <= len(ids) <= 256, "invalid stream_ids")
    for stream in ids:
        string(stream, "stream_id")
    require(len(set(ids)) == len(ids), "duplicate stream ID")
    streams = m.get("streams")
    require(isinstance(streams, list) and len(streams) == len(ids), "missing stream descriptors")
    descriptors = []
    for stream in streams:
        obj(stream, "stream descriptor")
        descriptors.append(string(stream.get("id"), "stream descriptor ID"))
        string(stream.get("device"), "device")
        string(stream.get("profile"), "profile")
        obj(stream.get("settings"), "stream settings")
    require(set(descriptors) == set(ids), "invalid stream descriptor IDs")
    count(m.get("sweep_count"), "sweep_count")
    count(m.get("observation_count"), "observation_count")
    require(set(obj(m.get("files"), "files")) == set(PAYLOADS), "invalid payload inventory")
    for name, receipt in m["files"].items():
        obj(receipt, "receipt")
        require(count(receipt.get("bytes"), "bytes") == archive.getinfo(name).file_size,
                f"{name} byte count mismatch")
        count(receipt.get("records"), "records")
        require(re.fullmatch(r"[0-9a-f]{64}", string(receipt.get("sha256"), "sha256")),
                "invalid SHA-256")
    return raw, m


def payload_lines(archive, name, receipt, destination):
    digest = hashlib.sha256()
    with ExitStack() as stack:
        data = stack.enter_context(archive.open(name))
        output = stack.enter_context((destination / name).open("xb")) if destination else None
        for line in iter(lambda: data.readline(MAX_LINE + 1), b""):
            require(len(line) <= MAX_LINE and line.endswith(b"\n"), f"{name} invalid line")
            digest.update(line)
            if output:
                output.write(line)
            yield line
    require(receipt["sha256"] == digest.hexdigest(), f"{name} SHA-256 mismatch")


def register(db, record, kind, manifest):
    stream = record.get("stream_id")
    require(record.get("session_id") == manifest["session_id"] and stream in manifest["stream_ids"],
            "unexpected record session or stream")
    db.execute("INSERT INTO ids VALUES(?,?,?)", (stream, kind, string(record.get("id"), "id")))
    return stream


def reference(db, stream, kind, value):
    if value is not None:
        db.execute("INSERT INTO refs VALUES(?,?,?)", (stream, kind, string(value, "reference")))


def measurements(db, lines, manifest):
    header = parse(next(lines, b""))
    require(header.get("format") == "openradioinspector-archive-ndjson"
            and count(header.get("format_version"), "NDJSON version") == 1
            and header.get("session_id") == manifest["session_id"], "invalid measurements header")
    counts = {"sweep": 0, "observation": 0}
    for line in lines:
        entry = parse(line)
        kind = entry.get("kind")
        require(kind in ("sweep", "observation"), "unknown measurement kind")
        m = obj(entry.get("measurement"), "measurement")
        stream = register(db, m, kind, manifest)
        interval = obj(m.get("interval"), "interval")
        require(utc(interval.get("started_at")) <= utc(interval.get("ended_at")), "reversed interval")
        string(m.get("level_unit"), "level_unit")
        if kind == "sweep":
            freq, levels = m.get("frequencies_hz"), m.get("levels")
            require(isinstance(freq, list) and isinstance(levels, list) and 0 < len(freq) == len(levels),
                    "invalid sweep shape")
            previous = 0
            for f, level in zip(freq, levels):
                require(count(f, "frequency") >= previous, "decreasing frequency grid")
                number(level, "level")
                previous = f
            metadata = obj(m.get("settings", {}), "settings")
        else:
            require(count(m.get("frequency_hz"), "frequency_hz") > 0, "invalid receiver frequency")
            string(m.get("mode"), "mode")
            if m.get("level") is not None:
                number(m["level"], "level")
            metadata = obj(m.get("metadata", {}), "metadata")
        reference(db, stream, "gps", metadata.get("field_fix_id"))
        counts[kind] += 1
    for kind, observed in counts.items():
        require(observed == manifest[f"{kind}_count"], f"{kind} count mismatch")
    return sum(counts.values())


def track_feature(event):
    xy = [number(event.get(k), k) for k in ("longitude", "latitude")]
    require(abs(xy[0]) <= 180 and abs(xy[1]) <= 90, "invalid geographic coordinates")
    if event.get("altitude_m") is not None:
        xy.append(number(event["altitude_m"], "altitude_m"))
    if event.get("accuracy_m") is not None:
        require(number(event["accuracy_m"], "accuracy_m") >= 0, "negative accuracy")
    omit = {"kind", "id", "longitude", "latitude", "altitude_m"}
    return {"type": "Feature", "id": event["id"], "geometry": {"type": "Point", "coordinates": xy},
            "properties": {k: v for k, v in event.items() if k not in omit}}


def events(db, lines, manifest):
    total = 0
    for line in lines:
        event = parse(line)
        kind = event.get("kind")
        require(kind in ("gps", "note", "gap"), "unknown event kind")
        stream = register(db, event, kind, manifest)
        utc(event.get("time_utc"))
        require(event.get("sweep_id") is None or event.get("observation_id") is None,
                "ambiguous measurement reference")
        for target in ("sweep", "observation"):
            reference(db, stream, target, event.get(f"{target}_id"))
        if kind == "gps":
            db.execute("INSERT INTO fixes VALUES(?)", (json.dumps(track_feature(event)),))
        else:
            key = "text" if kind == "note" else "reason"
            string(event.get(key), key)
        total += 1
    return total


def track(db, lines):
    require(next(lines, None) == TRACK_START, "invalid track header")
    total = 0
    for (expected,) in db.execute("SELECT feature FROM fixes ORDER BY rowid"):
        line = next(lines, b"")
        if total:
            require(line.startswith(b","), "missing track feature separator")
            line = line[1:]
        feature = parse(line)
        coords = obj(feature.get("geometry"), "geometry").get("coordinates")
        require(isinstance(coords, list) and all(type(x) in (float, int) for x in coords),
                "invalid track coordinates")
        require(feature == json.loads(expected), "track differs from GPS event")
        total += 1
    require(next(lines, None) == b"]}\n" and next(lines, None) is None, "invalid track trailer")
    return total


def validate(source: Path, destination: Path | None = None):
    """Read all members; optionally atomically publish verified contents in a new directory."""
    if destination is not None:
        require(not destination.exists() and not destination.is_symlink(), "destination already exists")
    parent = destination.parent if destination is not None else None
    with tempfile.TemporaryDirectory(dir=parent) as temp, source.open("rb") as file, \
            open_zip(file) as archive, closing(sqlite3.connect(Path(temp) / "references.sqlite")) as db:
        db.executescript("""
            CREATE TABLE ids(stream,kind,id,PRIMARY KEY(stream,kind,id)) WITHOUT ROWID;
            CREATE TABLE refs(stream,kind,id);
            CREATE TABLE fixes(feature);
        """)
        raw, manifest = read_manifest(archive)
        stage = Path(temp) / "contents" if destination is not None else None
        if stage:
            stage.mkdir()
            (stage / MANIFEST).write_bytes(raw)
        for name in PAYLOADS:
            receipt = manifest["files"][name]
            lines = payload_lines(archive, name, receipt, stage)
            with closing(lines):
                if name == MEASUREMENTS:
                    recorded = measurements(db, lines, manifest)
                elif name == EVENTS:
                    recorded = events(db, lines, manifest)
                else:
                    recorded = track(db, lines)
            require(recorded == receipt["records"], f"{name} record count mismatch")
        missing = db.execute("""SELECT 1 FROM refs LEFT JOIN ids USING(stream,kind,id)
                                WHERE ids.id IS NULL LIMIT 1""").fetchone()
        require(missing is None, "dangling field record reference")
        if stage:
            stage.rename(destination)
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("verify", "extract"):
        sub = commands.add_parser(command)
        sub.add_argument("archive", type=Path)
        if command == "extract":
            sub.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(validate(args.archive, getattr(args, "destination", None)), ensure_ascii=False))
    except (ValueError, TypeError, OverflowError, RecursionError, OSError, sqlite3.Error,
            zipfile.BadZipFile) as exc:
        print(f"Invalid field archive: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
