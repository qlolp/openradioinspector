"""Check one RF Explorer 10 kHz pass against its requested grid in a field export."""
import argparse
import json
import math
import sys
import zipfile


def verify(lines, stream_id, pass_id, start, stop):
    step, width = 10_000, 112
    if (stop - start) % step or not 1_110_000 <= stop - start <= 2_685_000_000:
        raise ValueError("invalid survey bounds")
    bins = (stop - start) // step + 1
    total = (bins + width - 1) // width
    header = json.loads(next(iter(lines), b"{}"))
    if header.get("format") != "openradioinspector-archive-ndjson" or header.get("format_version") != 1:
        raise ValueError("invalid NDJSON header")
    session = header["session_id"]
    seen, ids = set(), set()
    for line in lines:
        row = json.loads(line)
        if row.get("kind") != "sweep":
            continue
        record = row["measurement"]
        survey = record.get("settings", {}).get("survey", {})
        if record["stream_id"] != stream_id or survey.get("pass_id") != pass_id:
            continue
        index = survey["tile"]
        if (record["session_id"] != session or type(index) is not int or not 1 <= index <= total
                or index in seen or record["id"] in ids
                or (survey["start_hz"], survey["stop_hz"], survey["step_hz"], survey["tiles"]) != (start, stop, step, total)):
            raise ValueError("inconsistent or duplicate window")
        first = min(start + (index - 1) * width * step, stop - (width - 1) * step)
        hz = record["frequencies_hz"]
        if any(type(f) is not int for f in hz) or hz != list(range(first, first + width * step, step)):
            raise ValueError(f"incorrect frequency grid in window {index}")
        levels = record["levels"]
        if (record["level_unit"] != "dBm" or len(levels) != width
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in levels)):
            raise ValueError(f"invalid levels in window {index}")
        seen.add(index)
        ids.add(record["id"])
    if len(seen) != total:
        raise ValueError(f"incomplete pass: {len(seen)} of {total} windows")
    return dict(stream_id=stream_id, pass_id=pass_id, frequencies=bins, windows=total,
                overlap_points=total * width - bins)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("stream_id")
    parser.add_argument("pass_id")
    parser.add_argument("start_hz", type=int)
    parser.add_argument("stop_hz", type=int)
    args = parser.parse_args()
    try:
        with zipfile.ZipFile(args.archive) as z:
            if z.namelist().count("measurements.ndjson") != 1:
                raise ValueError("expected exactly one measurement stream")
            with z.open("measurements.ndjson") as data:
                report = verify(data, args.stream_id, args.pass_id, args.start_hz, args.stop_hz)
        print(json.dumps(report))
    except (KeyError, ValueError, TypeError, OSError, zipfile.BadZipFile) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
