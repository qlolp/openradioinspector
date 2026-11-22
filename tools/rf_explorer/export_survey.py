"""Export one complete, sequential RF Explorer survey pass from a v1 archive snapshot.

Obtain the snapshot with python3 -m radioinspector.archive_export_stream
--server URL --session SESSION --stream-id STREAM --output session.ndjson.
The CSV uses the earliest measurement at each frequency and preserves each
repeated tail sample alongside it. Point indices are zero-based.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


DETECTORS = ("normal", "max", "avg", "overwrite", "max_hold", "max_historical")
DETECTOR_LABELS = ("Normal", "Max", "Average", "Overwrite", "Max Hold", "Historical maximum")
MAX_LINE = 16 * 1024 * 1024 + 1024  # Archive record limit plus the NDJSON envelope.
PLOT_WIDTH = 1024
SOURCE = ("sweep_id", "point_index", "tile", "started_at", "ended_at", "gap_before_tile_s")
CSV_HEADER = (
    "frequency_hz", "level_dbm", *SOURCE,
    "overlap_level_dbm", *("overlap_" + key for key in SOURCE),
    "session_id", "stream_id", "pass_id", "pass", "active_board", "detector",
    "artifact_kind", "rbw_hz", "offset_db", "simulated",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(fields, name):
    value = fields.get(name)
    require(type(value) is int, f"{name} must be an integer")
    return value


def identifier(fields, name):
    value = fields.get(name)
    require(isinstance(value, str) and value.strip(), f"missing {name}")
    return value


def timestamp(value):
    require(isinstance(value, str), "missing interval timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(dt.tzinfo is not None, "interval timestamp requires a timezone")
    return dt.astimezone(timezone.utc)


def utc(dt):
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass
class Tile:
    number: int
    sweep_id: str
    stream_id: str
    session_id: str
    survey: dict
    readback: dict
    frequencies: list
    levels: list
    started: datetime
    ended: datetime
    gap: float | None = None

    @property
    def unique_points(self):
        return (self.survey["stop_hz"] - self.survey["start_hz"]) // self.survey["step_hz"] + 1

    def source(self):
        return self.number, utc(self.started), utc(self.ended), self.gap


def parse_tile(raw, settings, survey, session_id):
    start, stop, step = (integer(survey, k) for k in ("start_hz", "stop_hz", "step_hz"))
    number, tiles, pass_number = (integer(survey, k) for k in ("tile", "tiles", "pass"))
    points = integer(settings, "points")
    require(points == 112, "this survey mode requires 112 readback points")
    require(step == 10_000 and integer(settings, "step_hz") == step, "invalid survey/readback step")
    require(start % 1000 == 0 and stop > start and (stop - start) % step == 0, "invalid survey lattice")
    count = (stop - start) // step + 1
    require(count >= points and (count + points - 1) // points == tiles, "wrong number of tiles or points")
    require(1 <= number <= tiles and pass_number >= 1, "invalid tile or pass number")
    # A short tail shifts the last full-width window back onto the end of the requested grid.
    first = min(start + (number - 1) * points * step, stop - (points - 1) * step)
    require(integer(settings, "start_hz") == first, "start_hz readback differs from expected tile")
    last = first + (points - 1) * step
    require(integer(settings, "stop_hz") == last, "stop_hz readback differs from expected tile")
    frequencies, levels = raw.get("frequencies_hz"), raw.get("levels")
    require(isinstance(frequencies, list) and isinstance(levels, list) and
            len(frequencies) == len(levels) == points, "frequency/level array length differs from points")
    require(all(type(f) is int and f == first + i * step for i, f in enumerate(frequencies)),
            "frequency array differs from integer readback lattice")

    validate_readback(settings, start, stop)
    require(raw.get("level_unit") == "dBm", "RF Explorer requires dBm levels")
    offset = integer(settings, "offset_db")
    for level in levels:
        require(type(level) in (int, float) and offset - 127.5 <= level <= offset and
                ((offset - level) * 2) % 1 == 0, "invalid RF Explorer level")
    require(raw.get("session_id") == session_id, "session_id differs from archive header")
    interval = raw.get("interval")
    require(isinstance(interval, dict), "missing measurement interval")
    started, ended = (timestamp(interval.get(k)) for k in ("started_at", "ended_at"))
    require(ended >= started, "reversed host frame interval")
    return Tile(number, identifier(raw, "id"), identifier(raw, "stream_id"), session_id,
                {k: survey[k] for k in ("start_hz", "stop_hz", "step_hz", "pass", "tiles", "pass_id")},
                {k: v for k, v in settings.items() if k not in ("survey", "start_hz", "stop_hz")},
                frequencies, levels, started, ended)


def validate_readback(settings, start, stop):
    require(settings.get("adapter") == "rf-explorer-3g-serial" and
            settings.get("device_mode") == "spectrum_analyzer", "expected RF Explorer analyzer readback")
    require(settings.get("receive_profile") == "rf-explorer-3g-combo-standard-1.34",
            "unsupported RF Explorer receive profile")
    identity = settings.get("device_identity")
    require(isinstance(identity, dict) and identity.get("main_model") == 3 and
            identity.get("expansion_model") == 5 and identity.get("firmware") == "01.34",
            "unsupported RF Explorer device identity")
    require(settings.get("settings_readback") is True and settings.get("settings_source") == "device_readback"
            and settings.get("timing_source") == "host_frame_read", "missing device readback or host frame timing")
    require(type(settings.get("simulated")) is bool, "missing simulated flag")
    board = settings.get("active_board")
    require(board in ("main", "expansion"), "unknown active board")
    board_min = integer(settings, "board_min_hz")
    board_max = integer(settings, "board_max_hz")
    max_span = integer(settings, "board_max_span_hz")
    low, high, port = ((15_000_000, 2_700_000_000, "right") if board == "expansion"
                       else (240_000_000, 960_000_000, "left"))
    window_span = settings["stop_hz"] - settings["start_hz"]
    require(settings.get("active_input") == port and low <= board_min <= start < stop <= board_max <= high
            and window_span <= max_span <= min(600_000_000, board_max - board_min),
            "survey exceeds active board/input limits")
    code = integer(settings, "detector_code")
    require(0 <= code < len(DETECTORS) and settings.get("detector") == DETECTORS[code] and
            settings.get("artifact_kind") == ("raw_spectrum" if code == 0 else "processed_trace"),
            "inconsistent detector, detector_code or artifact_kind")
    require(3000 <= integer(settings, "rbw_hz") <= 600_000, "invalid rbw_hz readback")
    require(integer(settings, "amplitude_top_dbm") > integer(settings, "amplitude_bottom_dbm"),
            "invalid amplitude readback limits")
    require(settings.get("level_encoding") == "dBm = offset_db - u8 / 2", "unknown RF Explorer level encoding")


def selected_tiles(source, pass_id, digest):
    session_id = None
    with source.open("rb") as lines:
        for line_no, line in enumerate(iter(lambda: lines.readline(MAX_LINE + 1), b""), 1):
            try:
                require(len(line) <= MAX_LINE, "record exceeds the archive size limit")
                digest.update(line)
                value = json.loads(line)
                require(isinstance(value, dict), "expected an NDJSON object")
                if line_no == 1:
                    require(value.get("format") == "openradioinspector-archive-ndjson"
                            and integer(value, "format_version") == 1, "expected v1 archive NDJSON header")
                    session_id = identifier(value, "session_id")
                    continue
                raw = value.get("measurement")
                require(value.get("kind") in ("sweep", "observation") and isinstance(raw, dict),
                        "expected a v1 sweep or observation")
                if value["kind"] == "sweep":
                    settings = raw.get("settings", {})
                    require(isinstance(settings, dict), "invalid sweep settings")
                    survey = settings.get("survey")
                    if isinstance(survey, dict) and survey.get("pass_id") == pass_id:
                        yield parse_tile(raw, settings, survey, session_id)
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"archive line {line_no}: {exc}") from exc


def write_tile(writer, tile, skip, following, peaks):
    source = tile.source()
    repeat_source = following.source() if following else None
    settings, survey = tile.readback, tile.survey
    common = (tile.session_id, tile.stream_id, survey["pass_id"], survey["pass"], settings["active_board"],
              settings["detector"], settings["artifact_kind"], settings["rbw_hz"], settings["offset_db"],
              "true" if settings["simulated"] else "false")
    for i in range(skip, len(tile.levels)):
        frequency, level = tile.frequencies[i], tile.levels[i]
        repeated = (None,) * (1 + len(SOURCE))
        peak = level
        if following and frequency >= following.frequencies[0]:
            j = (frequency - following.frequencies[0]) // survey["step_hz"]
            repeated = (following.levels[j], following.sweep_id, j, *repeat_source)
            peak = max(peak, following.levels[j])
        writer.writerow((frequency, level, tile.sweep_id, i, *source, *repeated, *common))
        index = (frequency - survey["start_hz"]) // survey["step_hz"]
        bucket = index * len(peaks) // tile.unique_points
        if peaks[bucket] is None or peak > peaks[bucket][1]:
            peaks[bucket] = (frequency, peak)


def assemble(source, pass_id, output):
    digest = hashlib.sha256()
    tiles = selected_tiles(source, pass_id, digest)
    first = next(tiles, None)
    require(first is not None, "no sweeps found for requested pass ID")
    require(first.number == 1, f"incomplete pass begins at tile {first.number}")
    seen = {first.sweep_id}
    writer = csv.writer(output)
    writer.writerow(CSV_HEADER)
    peaks = [None] * min(first.unique_points, PLOT_WIDTH)
    previous = first
    skip = 0
    frame_time = first.ended - first.started
    max_gap = timedelta()
    gaps = 0
    for tile in tiles:
        require(tile.number > previous.number and tile.sweep_id not in seen,
                f"duplicate tile or sweep ID at tile {tile.number}")
        require(tile.number == previous.number + 1, f"incomplete pass before tile {tile.number}")
        require(tile.stream_id == first.stream_id, "mixed stream_id in pass")
        require(tile.survey == first.survey, "mixed survey configuration or pass number")
        keys = first.readback.keys() | tile.readback.keys()
        changed = [k for k in keys if tile.readback.get(k) != first.readback.get(k)]
        require(not changed, "mixed readback settings: " + ", ".join(sorted(changed)))
        seen.add(tile.sweep_id)
        gap = tile.started - previous.ended
        require(gap >= timedelta(), f"overlapping or reversed time at tile {tile.number}")
        tile.gap = gap.total_seconds()
        max_gap = max(max_gap, gap)
        gaps += gap > timedelta()
        frame_time += tile.ended - tile.started
        write_tile(writer, previous, skip, tile, peaks)
        skip = max(0, (previous.frequencies[-1] - tile.frequencies[0]) // first.survey["step_hz"] + 1)
        previous = tile
    require(previous.number == first.survey["tiles"],
            f"incomplete pass: received {previous.number} of {first.survey['tiles']} tiles")
    write_tile(writer, previous, skip, None, peaks)
    wall = previous.ended - first.started
    raw_points = len(seen) * first.readback["points"]
    fields = ("active_board", "active_input", "detector", "artifact_kind", "rbw_hz", "offset_db", "simulated")
    summary = {
        **first.survey, **{key: first.readback[key] for key in fields},
        "session_id": first.session_id, "stream_id": first.stream_id, "archive_sha256": digest.hexdigest(),
        "unique_points": first.unique_points, "raw_points": raw_points,
        "overlap_points": raw_points - first.unique_points,
        "started_at": utc(first.started), "ended_at": utc(previous.ended), "wall_seconds": wall.total_seconds(),
        "frame_read_seconds": frame_time.total_seconds(), "inter_tile_gap_seconds": (wall - frame_time).total_seconds(),
        "inter_tile_gap_count": gaps, "max_inter_tile_gap_seconds": max_gap.total_seconds(),
    }
    return summary, [p for p in peaks if p is not None]


def write_svg(path, s, peaks):
    left, top, width, height = 82, 125, PLOT_WIDTH, 286
    svg = ET.Element("svg", xmlns="http://www.w3.org/2000/svg", viewBox="0 0 1150 550",
                     width="1150", height="550", role="img")
    ET.SubElement(svg, "title").text = "RF Explorer sequential survey panorama"
    ET.SubElement(svg, "desc").text = (
        f"Sequential host frame readings for pass {s['pass_id']}. The CSV identifies every source sweep."
    )
    ET.SubElement(svg, "metadata").text = json.dumps(s, ensure_ascii=False)

    def elem(tag, **attrs):
        return ET.SubElement(svg, tag, {k.replace("_", "-"): str(v) for k, v in attrs.items()})

    def label(x, y, value, size=14, **attrs):
        elem("text", x=x, y=y, font_family="sans-serif", font_size=size, fill="#1d3240", **attrs).text = value

    elem("rect", x=0, y=0, width="100%", height="100%", fill="white")
    label(left, 35, "RF Explorer sequential survey panorama", 25, font_weight="bold")
    detector = DETECTOR_LABELS[DETECTORS.index(s["detector"])]
    board = "WSUB3G" if s["active_board"] == "expansion" else "WSUB1G"
    label(left, 64, f"{board} ({s['active_input']} input)   ·   {detector} ({s['artifact_kind'].replace('_', ' ')})"
          f"   ·   RBW {s['rbw_hz']/1000:g} kHz   ·   Grid {s['step_hz']/1000:g} kHz"
          + ("   ·   Simulated data" if s["simulated"] else ""))
    label(left, 88, f"{s['unique_points']:,} measured frequencies   ·   {s['tiles']:,} tiles   ·   "
          f"{s['overlap_points']} repeated samples   ·   Maximum per display column, including repeats")
    elem("rect", id="plot-area", x=left, y=top, width=width, height=height, fill="#f5f8fa")
    levels = [p[1] for p in peaks]
    low, high = math.floor(min(levels) / 10) * 10, math.ceil(max(levels) / 10) * 10
    if low == high:
        low -= 10
    for tick in range(6):
        fraction = tick / 5
        y = top + height * (1 - fraction)
        elem("line", x1=left, y1=y, x2=left+width, y2=y, stroke="#d9e2e7")
        label(left-10, y+5, f"{low+(high-low)*fraction:g}", 13, text_anchor="end")
        x = left + width * fraction
        frequency = s["start_hz"] + (s["stop_hz"]-s["start_hz"]) * fraction
        label(x, top+height+22, f"{frequency/1_000_000:.4f}".rstrip("0").rstrip("."), 13, text_anchor="middle")
    label(left-10, top-8, "dBm", 13, text_anchor="end")
    label(left+width/2, top+height+45, "Frequency (MHz)", text_anchor="middle")
    pairs = []
    for frequency, level in peaks:
        x = left + width * (frequency-s["start_hz"]) / (s["stop_hz"]-s["start_hz"])
        y = top + height * (high-level) / (high-low)
        pairs.append(f"{x:.2f},{y:.2f}")
    elem("polyline", id="peak-envelope", fill="none", stroke="#16607f", stroke_width=1.4,
         stroke_linejoin="round", points=" ".join(pairs))
    label(left, 482, f"Host UTC   {s['started_at']}  —  {s['ended_at']}")
    label(left, 508, f"Elapsed {s['wall_seconds']:.6f} s   ·   Frame reads {s['frame_read_seconds']:.6f} s   ·   "
          f"Between frames {s['inter_tile_gap_seconds']:.6f} s across {s['inter_tile_gap_count']:,} gaps")
    ET.ElementTree(svg).write(path, encoding="utf-8", xml_declaration=True)


def export(source, pass_id, csv_path, svg_path):
    paths = [p.resolve() for p in (source, csv_path, svg_path)]
    require(len(set(paths)) == 3, "input, CSV and SVG need distinct paths")
    for path in (csv_path, svg_path):
        require(not path.is_dir(), f"output is a directory: {path}")
    with TemporaryDirectory(dir=csv_path.parent) as csv_dir, TemporaryDirectory(dir=svg_path.parent) as svg_dir:
        csv_temp, svg_temp = Path(csv_dir) / "panorama.csv", Path(svg_dir) / "panorama.svg"
        with csv_temp.open("w", encoding="utf-8", newline="") as output:
            summary, peaks = assemble(source, pass_id, output)
        write_svg(svg_temp, summary, peaks)
        # Publish the small figure first so we can restore it if the CSV is locked.
        previous_svg = Path(svg_dir) / "previous.svg"
        if svg_path.exists() or svg_path.is_symlink():
            shutil.copy2(svg_path, previous_svg, follow_symlinks=False)
        svg_temp.replace(svg_path)
        try:
            csv_temp.replace(csv_path)
        except OSError:
            if previous_svg.exists() or previous_svg.is_symlink():
                previous_svg.replace(svg_path)
            else:
                svg_path.unlink()
            raise
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="v1 archive NDJSON snapshot")
    parser.add_argument("--pass-id", required=True, help="exact settings.survey.pass_id")
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--svg", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = export(args.input, args.pass_id, args.csv, args.svg)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Survey export failed: {exc}\n")
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
