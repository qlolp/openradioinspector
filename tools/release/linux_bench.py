"""Check a Debian receipt against retained benchmark reports and the original USB archive export."""

from contextlib import closing
import hashlib
import math
from pathlib import Path
import sqlite3

from benchmarks.ori_bench.client import LINE_LIMIT
from benchmarks.ori_bench.paged_history import active
from benchmarks.ori_bench.scan_rate import MODEL, timestamp
from tools.modernization.api_parity import load_json
from .macos_rc_package import digest, read_json
from .release_artifacts import require, sha


def file_with_hash(path, expected):
    require(
        sha(expected) and path.is_file() and not path.is_symlink() and digest(path) == expected,
        "missing or changed private Debian evidence: " + path.name,
    )
    return path


def matches(record, expected, label):
    for key, value in expected.items():
        actual = record.get(key)
        if type(value) in (int, float):
            equal = type(actual) in (int, float) and actual == value
        else:
            equal = type(actual) is type(value) and actual == value
        require(equal, f"{label}: {key} differs from the source evidence")


def producer_counts(report, kind, count):
    matches(
        report["counts"][kind],
        dict(
            offered=count,
            acknowledged=count,
            verified=count,
            rejected=0,
            queue_dropped=0,
            transport_unknown=0,
            injected_gap=0,
            cancelled=0,
            invalid_ack=0,
        ),
        "producer " + kind,
    )


def native_host(report):
    host = report["host"]
    require(
        host["platform"].startswith("Linux-") and host["machine"] in ("x86_64", "amd64"),
        "benchmark requires its native Linux amd64 host report",
    )


def ledger_counts(path, workload, offered, bracket):
    require(
        path.is_file() and not path.is_symlink() and not Path(str(path) + "-wal").exists(),
        "completed private offer ledger required",
    )
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        require(
            db.execute("PRAGMA application_id").fetchone() == (0x4F524942,)
            and db.execute("PRAGMA user_version").fetchone() == (1,),
            "unknown producer ledger",
        )
        rows = db.execute(
            """
            SELECT kind, COUNT(*), SUM(status='accepted' AND verified=1),
                   SUM(frames), SUM(frames * (listed=1)), MIN(offered_ns), MAX(offered_ns),
                   SUM(id != ? || '-' || kind || '-' || printf('%09d', idx))
            FROM offers GROUP BY kind
        """,
            (workload["run_id"],),
        ).fetchall()
    require({r[0] for r in rows} == {"sweep", "observation", "audio"}, "missing source kind in producer ledger")
    for kind, count, verified, frames, listed, first, last, foreign in rows:
        require(
            count == verified == workload["planned_counts"][kind] and foreign == 0,
            "offer ledger includes a foreign, missing or unverified " + kind,
        )
        if kind == "audio":
            require(frames == listed == offered["pcm_samples"], "offered or retained PCM differs from the ledger")
        elif kind == "sweep":
            require([first, last] == bracket, "background operation was not bracketed by this producer")


def check_producer(producer, outer, load):
    matches(producer, dict(run_status="completed", errors=[], accelerated_source_time=False), "producer")
    native_host(producer)
    workload = producer["workload"]
    require(outer["workload"] == workload, "producer and background workload differ")
    matches(
        workload,
        dict(
            mode="run",
            scenario="stress",
            speed=1,
            points=load["points_per_sweep"],
            observer_clients=3,
            observe_render=True,
            audio_sample_rate=load["pcm_rate_hz"],
            audio_channels=load["pcm_channels"],
            audio_sample_bytes=2,
            planned_audio_frames=load["offered"]["pcm_samples"],
        ),
        "producer workload",
    )
    matches(
        producer["production"],
        dict(production_elapsed_seconds=load["seconds"], scheduled_wall_seconds=1800),
        "measured production duration",
    )
    checks = producer["checks"]
    for name in ("complete_delivery", "archive_integrity", "offered_accounting", "render_latency"):
        require(checks[name]["status"] == "passed", "producer source check failed: " + name)
    require(outer["producer_complete_delivery"] == checks["complete_delivery"], "producer outcome differs")
    planned = workload["planned_counts"]
    for kind, field in (("sweep", "sweeps"), ("observation", "observations")):
        require(
            planned[kind] == load["offered"][field]
            and checks["archive_integrity"]["source_counts"][kind] == load["archived"][field],
            "offered or archived " + kind + " count differs",
        )
        producer_counts(producer, kind, planned[kind])
    producer_counts(producer, "audio", planned["audio"])
    audio = producer["audio"]
    matches(
        audio,
        dict(
            offered_frames=load["offered"]["pcm_samples"],
            acknowledged_frames=load["offered"]["pcm_samples"],
            verified_frames=load["archived"]["pcm_samples"],
            listed_segments=planned["audio"],
            all_offered_pcm_preserved=True,
        ),
        "original PCM",
    )
    require(
        sha(audio["expected_pcm_sha256"]) and audio["expected_pcm_sha256"] == audio["retained_pcm_sha256"],
        "verified PCM bytes differ",
    )
    rendered = checks["render_latency"]
    matches(
        rendered,
        dict(
            observer_completed=True,
            observer_dropped=0,
            clients=3,
            observed_unique_renders=3 * planned["sweep"],
            expected_renders=3 * planned["sweep"],
        ),
        "render observer",
    )


def check_background(outer, load, folder):
    matches(
        outer,
        dict(
            successful=True,
            run_status="passed",
            errors=[],
            producer_status=0,
            history_profile="paged-history",
            full_30_minute_reference=True,
            actual_30_minute_run_observed=True,
            producer_report="producer/report.json",
        ),
        "background benchmark",
    )
    require(sha(outer["candidate_sha256"]), "missing benchmark fixture executable digest")
    workload = outer["workload"]
    planned = workload["planned_counts"]
    background = outer["background"]
    matches(background, dict(status="passed", errors=[], threads_joined=True), "background work")
    require(
        background["history"]["cursor_pages"] > 0
        and background["history"]["verified_summaries"] > 0
        and load["online_history_verified"] is True,
        "missing original cursor-paged history",
    )
    export = background["live_export"]
    require(
        load["online_export_verified"] is True
        and export["phase"] == "completed"
        and export["status"] == "passed"
        and sha(export["ndjson_sha256"])
        and all(0 < export["source_counts"][k] <= planned[k] for k in ("sweep", "observation")),
        "missing original export during acquisition",
    )
    backup = background["backup"]
    restored = outer["restore"]
    require(
        load["online_backup_verified"] is True
        and backup["phase"] == "completed"
        and load["backup_restore_verified"] is True
        and restored["status"] == "passed"
        and restored["joined"] is True
        and restored["manifest"] == backup["manifest"]
        and restored["manifest"]["archive_schema"] == 7,
        "restore differs from the original online backup",
    )
    observed = background["overlap"]
    bracket = observed["verified_offers_bracket_ns"]
    require(
        active(observed["before"])
        and active(observed["after"])
        and bracket[0] <= observed["started_monotonic_ns"] <= observed["finished_monotonic_ns"] <= bracket[1],
        "history, backup and export did not overlap the producer",
    )
    ledger_counts(folder / "three-client-ledger.sqlite", workload, load["offered"], bracket)


def check_history(history, producer, load):
    workload = producer["workload"]
    native_host(history)
    matches(history["host"], {k: producer["host"][k] for k in ("platform", "machine")}, "history host")
    matches(history, dict(run_status="completed", errors=[]), "history benchmark")
    matches(
        history["workload"],
        dict(mode="history", points=load["history_points_per_sweep"], sweeps=load["history_sweeps"]),
        "million-sweep workload",
    )
    require(history["workload"]["run_id"] != workload["run_id"], "history requires its separate million-sweep run")
    producer_counts(history, "sweep", load["history_sweeps"])
    summary = history["checks"]["history_summary_p95"]
    require(
        history["checks"]["archive_integrity"]["source_counts"]["sweep"] == load["history_sweeps"]
        and history["checks"]["archive_integrity"]["status"] == "passed"
        and history["checks"]["complete_delivery"]["status"] == "passed"
        and len(set(history["history"]["first_page_source_ids"])) == 100
        and history["history"]["endpoint_kind"] == "summaries"
        and history["history"]["warm_cache_requested"] is True
        and summary["status"] == "passed"
        and summary["reference_workload_matched"] is True
        and summary["measured_p95_upper_ms"]
        == history["latency"]["history_summary_ms"]["p95_upper_ms"]
        == load["history_first_100_ms"],
        "million-sweep warm history does not reproduce the receipt",
    )


def check_ui(row, folder, manifest, outer, history):
    workload = outer["workload"]
    load = row["loads"]["three-clients"]
    # The stock Chromium observer timestamps offers and draws. The distinct installed-UI
    # measurements are supplied by the bench operator with the trace from their instrument.
    protocol = read_json(file_with_hash(folder / "protocol.json", row["protocol_report_sha256"]), 16 << 20)
    ui = protocol["linux_ui"]
    matches(
        ui,
        dict(
            status="operator_observed",
            artifact_sha256=row["artifact_sha256"],
            core_sha256=manifest["components"]["ori-core"],
            ui_sha256=manifest["components"]["ui"],
            run_id=workload["run_id"],
            history_run_id=history["workload"]["run_id"],
            benchmark_fixture_sha256=outer["candidate_sha256"],
        ),
        "installed UI measurement",
    )
    require(isinstance(ui["method"], str) and bool(ui["method"].strip()), "UI timing method is unmeasured")
    trace = file_with_hash(folder / "linux-ui-trace", ui["trace_sha256"])
    require(
        trace.stat().st_size > 0
        and ui["trace_sha256"]
        not in (load["report_sha256"], load["producer_report_sha256"], load["history_report_sha256"]),
        "separate installed UI timing trace required",
    )
    matches(
        ui,
        {k: load[k] for k in ("reaction_p95_ms", "queue_p95_ms", "commit_to_display_p95_ms", "pan_min_fps")},
        "operator-measured UI metrics",
    )


def verify_load(row, folder, manifest):
    """Read the original paged-history bench, producer, ledger and independent million-sweep run."""
    try:
        load = row["loads"]["three-clients"]
        outer = read_json(file_with_hash(folder / "load-three-clients.json", load["report_sha256"]), 16 << 20)
        producer = read_json(
            file_with_hash(folder / "producer/report.json", load["producer_report_sha256"]), 16 << 20
        )
        history = read_json(
            file_with_hash(folder / "history/report.json", load["history_report_sha256"]), 16 << 20
        )
        check_producer(producer, outer, load)
        check_background(outer, load, folder)
        check_history(history, producer, load)
        check_ui(row, folder, manifest, outer, history)
    except (KeyError, IndexError, AttributeError, TypeError, OSError, sqlite3.DatabaseError) as error:
        raise ValueError("incomplete private Debian load evidence") from error


def allowed_pause(previous, end, limit, gaps):
    if (end - previous).total_seconds() <= limit:
        return True
    # Both margins must be covered by the measured cadence of this channel.
    return any(
        previous <= stop
        and begin <= end
        and ((max(previous, begin) - previous) + (end - min(end, stop))).total_seconds() <= limit
        for begin, stop in gaps
    )


def verify_hardware(row, folder, manifest):
    """Check both physical stream timelines and the SHA-256 of the original session NDJSON."""
    try:
        signed = row["hardware"]
        hw = read_json(file_with_hash(folder / "hardware.json", signed["report_sha256"]))
        matches(
            hw,
            dict(
                source_revision=row["source_revision"],
                artifact_sha256=row["artifact_sha256"],
                core_sha256=manifest["components"]["ori-core"],
                coverage_sha256=signed["coverage_sha256"],
            ),
            "physical run identity",
        )
        path = folder / "hardware-coverage.jsonl"
        require(path.is_file() and not path.is_symlink(), "original physical export is missing")
        streams = {"sweep": hw["analyzer_stream_id"], "observation": hw["receiver_stream_id"]}
        require(
            all(isinstance(s, str) and s for s in streams.values()) and len(set(streams.values())) == 2,
            "two distinct hardware streams are required",
        )
        limits = hw["max_silence_seconds"]
        gaps = {kind: [] for kind in streams}
        for kind in streams:
            require(
                type(limits[kind]) in (int, float) and math.isfinite(limits[kind]) and 0 < limits[kind] <= 3600,
                "missing physical profile revisit or dwell limit",
            )
        for gap in hw.get("planned_gaps", []):
            kind = gap["kind"]
            begin, end = timestamp(gap["started_at"]), timestamp(gap["ended_at"])
            require(
                kind in streams and gap["reason"] == "usb_reconnect" and 0 < (end - begin).total_seconds() <= 300,
                "invalid planned USB interruption",
            )
            gaps[kind].append((begin, end))
        first, last = {}, {}
        ids = {kind: set() for kind in streams}
        digestor = hashlib.sha256()
        with path.open("rb") as source:
            for index, line in enumerate(iter(lambda: source.readline(LINE_LIMIT + 1), b"")):
                require(len(line) <= LINE_LIMIT and line.endswith(b"\n"), "oversized or truncated source")
                digestor.update(line)
                entry = load_json(line, LINE_LIMIT)
                if index == 0:
                    require(
                        entry
                        == dict(
                            format="openradioinspector-archive-ndjson",
                            format_version=1,
                            session_id=hw["session_id"],
                        ),
                        "unexpected physical session archive header",
                    )
                    continue
                kind, item = entry["kind"], entry["measurement"]
                require(
                    kind in streams
                    and item["stream_id"] == streams[kind]
                    and item["session_id"] == hw["session_id"],
                    "foreign physical source",
                )
                identifier = item["id"]
                require(
                    isinstance(identifier, str) and identifier and identifier not in ids[kind],
                    "duplicate or empty physical source ID",
                )
                ids[kind].add(identifier)
                begin, end = timestamp(item["interval"]["started_at"]), timestamp(item["interval"]["ended_at"])
                require(
                    begin <= end and (end - begin).total_seconds() <= limits[kind],
                    "invalid physical measurement interval",
                )
                if kind == "sweep":
                    provenance = item["settings"]
                    identity = provenance["device_identity"]
                    require(
                        provenance["adapter"] == "tinysa-serial"
                        and item["level_unit"] == "dBm"
                        and isinstance(identity["firmware"], str)
                        and identity["firmware"].strip()
                        and any(
                            line.strip().casefold() in ("tinysa ultra", MODEL.casefold())
                            for line in identity["info"].splitlines()
                        )
                        and len(item["levels"]) == len(item["frequencies_hz"]) > 0,
                        "missing physical tinySA sweep",
                    )
                else:
                    provenance = item["metadata"]
                    require(
                        provenance["adapter"] == "ardv1-serial"
                        and isinstance(provenance["receiver_firmware"], str)
                        and provenance["receiver_firmware"].strip()
                        and item["level_unit"] == "s_meter_count"
                        and type(item["level"]) in (int, float)
                        and math.isfinite(item["level"]),
                        "missing physical AR-DV1 observation",
                    )
                require(
                    provenance["simulated"] is False and provenance.get("replay", False) is False,
                    "simulated or replayed measurement in physical coverage",
                )
                if kind in last:
                    require(
                        end >= last[kind] and allowed_pause(last[kind], end, limits[kind], gaps[kind]),
                        "physical stream time regressed or has an unexplained gap",
                    )
                first.setdefault(kind, end)
                last[kind] = end
        require(digestor.hexdigest() == signed["coverage_sha256"], "physical coverage export hash differs")
        require(
            all(
                len(ids[k]) == signed[field] and len(ids[k]) >= 2
                for k, field in (("sweep", "sweeps"), ("observation", "observations"))
            ),
            "physical source totals differ",
        )
        overlap = (min(last.values()) - max(first.values())).total_seconds()
        require(
            28_800 <= signed["seconds"] <= overlap < signed["seconds"] + 1,
            "the two original streams do not cover the claimed simultaneous eight-hour interval",
        )
    except (KeyError, IndexError, AttributeError, TypeError, OSError) as error:
        raise ValueError("incomplete original Debian hardware evidence") from error
