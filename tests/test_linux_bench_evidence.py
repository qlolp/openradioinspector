"""Compare the public Linux receipt with native producer, restore, history and archive evidence."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from tools.release import linux_bench as bench
from tools.release.macos_rc_package import digest, write_json


SHA = "a" * 64
GIT = "b" * 40


def counts(number):
    return dict(
        offered=number,
        verified=number,
        acknowledged=number,
        rejected=0,
        queue_dropped=0,
        transport_unknown=0,
        injected_gap=0,
        cancelled=0,
        invalid_ack=0,
    )


class LinuxLoadEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.ui = dict(reaction_p95_ms=23.0, queue_p95_ms=110.0, commit_to_display_p95_ms=65.0, pan_min_fps=60)
        self.planned = dict(sweep=18000, observation=18000, audio=180)
        totals = dict(sweeps=18000, observations=18000, pcm_samples=86_400_000)
        self.load = dict(
            seconds=1800.15,
            points_per_sweep=4096,
            pcm_rate_hz=48000,
            pcm_channels=1,
            offered=totals,
            archived=totals,
            online_history_verified=True,
            online_export_verified=True,
            online_backup_verified=True,
            backup_restore_verified=True,
            history_points_per_sweep=290,
            history_sweeps=1_000_000,
            history_first_100_ms=11.5,
            **self.ui,
        )
        self.row = dict(source_revision=GIT, artifact_sha256=SHA, loads={"three-clients": self.load})
        self.manifest = dict(components={"ori-core": SHA, "ui": SHA})
        workload = dict(
            run_id="bench-example",
            mode="run",
            scenario="stress",
            speed=1,
            points=4096,
            observer_clients=3,
            observe_render=True,
            audio_sample_rate=48000,
            audio_channels=1,
            audio_sample_bytes=2,
            planned_counts=self.planned,
            planned_audio_frames=86_400_000,
        )
        passed = {"status": "passed"}
        source_counts = dict(sweep=18000, observation=18000)
        host = dict(platform="Linux-7.0-x86_64", machine="x86_64")
        self.producer = dict(
            run_status="completed",
            errors=[],
            host=host,
            accelerated_source_time=False,
            production=dict(production_elapsed_seconds=1800.15, scheduled_wall_seconds=1800),
            workload=workload,
            counts={k: counts(n) for k, n in self.planned.items()},
            audio=dict(
                offered_frames=86_400_000,
                acknowledged_frames=86_400_000,
                verified_frames=86_400_000,
                listed_segments=180,
                all_offered_pcm_preserved=True,
                expected_pcm_sha256=SHA,
                retained_pcm_sha256=SHA,
            ),
            checks=dict(
                complete_delivery=passed,
                archive_integrity=dict(**passed, source_counts=source_counts),
                offered_accounting=passed,
                render_latency=dict(
                    **passed,
                    observer_completed=True,
                    observer_dropped=0,
                    clients=3,
                    observed_unique_renders=54000,
                    expected_renders=54000,
                    committed_to_render_measured=False,
                ),
            ),
        )
        snapshot = dict(
            qos=dict(registered_work=2, cleanup_failed=False, acquisition_pressure=False),
            export=dict(phase="streaming", verified_records=1),
            backup={"phase": "running"},
        )
        backup = dict(archive_schema=7, backend_id="owned-fixture")
        self.outer = dict(
            successful=True,
            run_status="passed",
            errors=[],
            producer_status=0,
            history_profile="paged-history",
            full_30_minute_reference=True,
            actual_30_minute_run_observed=True,
            producer_report="producer/report.json",
            candidate_sha256="c" * 64,
            producer_complete_delivery=passed,
            workload=workload,
            background=dict(
                status="passed",
                errors=[],
                threads_joined=True,
                history=dict(cursor_pages=1, verified_summaries=20),
                live_export=dict(
                    phase="completed",
                    status="passed",
                    ndjson_sha256=SHA,
                    source_counts={"sweep": 5, "observation": 5},
                ),
                backup=dict(phase="completed", manifest=backup),
                overlap=dict(
                    before=snapshot,
                    after=snapshot,
                    started_monotonic_ns=10,
                    finished_monotonic_ns=11,
                    verified_offers_bracket_ns=[0, 17999],
                ),
            ),
            restore=dict(status="passed", joined=True, manifest=backup),
        )
        self.history = dict(
            run_status="completed",
            errors=[],
            host=host,
            workload=dict(run_id="history-example", mode="history", points=290, sweeps=1_000_000),
            counts=dict(sweep=counts(1_000_000)),
            checks=dict(
                complete_delivery=passed,
                archive_integrity=dict(**passed, source_counts={"sweep": 1_000_000}),
                history_summary_p95=dict(**passed, reference_workload_matched=True, measured_p95_upper_ms=11.5),
            ),
            history=dict(
                first_page_source_ids=[f"id-{i}" for i in range(100)],
                endpoint_kind="summaries",
                warm_cache_requested=True,
            ),
            latency={"history_summary_ms": {"p95_upper_ms": 11.5}},
        )
        trace = self.folder / "linux-ui-trace"
        trace.write_bytes(b"private synthetic evidence for test")
        self.protocol = dict(
            linux_ui=dict(
                status="operator_observed",
                artifact_sha256=SHA,
                core_sha256=SHA,
                ui_sha256=SHA,
                run_id=workload["run_id"],
                history_run_id="history-example",
                benchmark_fixture_sha256="c" * 64,
                method="instrument fixture",
                trace_sha256=digest(trace),
                **self.ui,
            )
        )
        db = sqlite3.connect(self.folder / "three-client-ledger.sqlite")
        with db:
            db.executescript(
                "PRAGMA application_id=1330792770; PRAGMA user_version=1; "
                "CREATE TABLE offers(id,kind,idx,status,verified,frames,listed,offered_ns)"
            )
            for kind, count in self.planned.items():
                frames = 480000 if kind == "audio" else 0
                db.executemany(
                    "INSERT INTO offers VALUES(?,?,?,?,?,?,?,?)",
                    (
                        (f"bench-example-{kind}-{i:09d}", kind, i, "accepted", 1, frames, int(kind == "audio"), i)
                        for i in range(count)
                    ),
                )
        db.close()

    def check(self):
        files = [
            (self.load, "report_sha256", "load-three-clients.json", self.outer),
            (self.load, "producer_report_sha256", "producer/report.json", self.producer),
            (self.load, "history_report_sha256", "history/report.json", self.history),
            (self.row, "protocol_report_sha256", "protocol.json", self.protocol),
        ]
        for signed, field, name, value in files:
            path = self.folder / name
            path.parent.mkdir(exist_ok=True)
            write_json(path, value)
            signed[field] = digest(path)
        bench.verify_load(self.row, self.folder, self.manifest)

    def test_complete_reports_and_separate_installed_ui_witness(self):
        self.check()

    def test_failed_mismatched_or_unmeasured_private_reports_are_rejected_even_if_signed(self):
        mutations = [
            (self.outer, "successful", False),
            (self.outer, "restore", {}),
            (self.producer["audio"], "verified_frames", 1),
            (self.producer["counts"]["observation"], "offered", 17999),
            (self.history["latency"]["history_summary_ms"], "p95_upper_ms", 42.0),
            (self.protocol["linux_ui"], "status", "unmeasured"),
            (self.protocol["linux_ui"], "commit_to_display_p95_ms", 12),
        ]
        for target, key, value in mutations:
            previous = target[key]
            with self.subTest(key=key):
                target[key] = value
                with self.assertRaises(ValueError):
                    self.check()
                target[key] = previous

    def test_empty_or_different_run_does_not_pass_with_current_report_hash(self):
        self.outer = {}
        with self.assertRaises(ValueError):
            self.check()


class PhysicalDebianCoverageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.signed = dict(seconds=28800, sweeps=481, observations=481)
        self.row = dict(source_revision=GIT, artifact_sha256=SHA, hardware=self.signed)
        self.hw = dict(
            source_revision=GIT,
            artifact_sha256=SHA,
            core_sha256=SHA,
            session_id="session",
            analyzer_stream_id="analyzer",
            receiver_stream_id="receiver",
            max_silence_seconds={"sweep": 61, "observation": 61},
        )
        self.rows = [dict(format="openradioinspector-archive-ndjson", format_version=1, session_id="session")]
        for kind, stream in [("sweep", "analyzer"), ("observation", "receiver")]:
            for i in range(481):
                stamp = (self.start + timedelta(minutes=i)).isoformat()
                item = dict(
                    id=kind + str(i),
                    stream_id=stream,
                    session_id="session",
                    interval=dict(started_at=stamp, ended_at=stamp),
                )
                if kind == "sweep":
                    item.update(
                        settings=dict(
                            adapter="tinysa-serial",
                            simulated=False,
                            device_identity=dict(info="tinySA Ultra", firmware="1.4"),
                        ),
                        level_unit="dBm",
                        levels=[-55],
                        frequencies_hz=[100_000_000],
                    )
                else:
                    item.update(
                        metadata=dict(adapter="ardv1-serial", receiver_firmware="2504A", simulated=False),
                        level_unit="s_meter_count",
                        level=13,
                    )
                self.rows.append(dict(kind=kind, measurement=item))

    def check(self):
        path = self.folder / "hardware-coverage.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        self.hw["coverage_sha256"] = self.signed["coverage_sha256"] = digest(path)
        metadata = self.folder / "hardware.json"
        write_json(metadata, self.hw)
        self.signed["report_sha256"] = digest(metadata)
        bench.verify_hardware(self.row, self.folder, dict(components={"ori-core": SHA}))

    def test_both_original_streams_cover_the_same_eight_hours(self):
        self.check()

    def test_full_but_nonoverlapping_streams_rejected(self):
        for row in self.rows[482:]:
            interval = row["measurement"]["interval"]
            for key, value in interval.items():
                interval[key] = (datetime.fromisoformat(value) + timedelta(hours=8)).isoformat()
        with self.assertRaisesRegex(ValueError, "simultaneous"):
            self.check()

    def test_acknowledged_usb_gap_does_not_cover_the_other_instrument(self):
        gap = self.rows.pop(10)["measurement"]["interval"]["ended_at"]
        self.signed["sweeps"] -= 1
        with self.assertRaisesRegex(ValueError, "unexplained"):
            self.check()
        self.hw["planned_gaps"] = [
            dict(
                kind="observation",
                reason="usb_reconnect",
                started_at=gap,
                ended_at=(datetime.fromisoformat(gap) + timedelta(seconds=60)).isoformat(),
            )
        ]
        with self.assertRaisesRegex(ValueError, "unexplained"):
            self.check()
        self.hw["planned_gaps"][0]["kind"] = "sweep"
        self.check()

    def test_simulated_sources_and_disagreeing_counts_are_rejected(self):
        self.signed["sweeps"] -= 1
        with self.assertRaisesRegex(ValueError, "totals"):
            self.check()
        self.signed["sweeps"] += 1
        self.rows[2]["measurement"]["settings"]["simulated"] = True
        with self.assertRaisesRegex(ValueError, "simulated"):
            self.check()

    def test_replay_and_incidental_model_substrings_are_rejected(self):
        settings = self.rows[1]["measurement"]["settings"]
        settings["replay"] = True
        with self.assertRaisesRegex(ValueError, "replayed"):
            self.check()
        del settings["replay"]
        settings["device_identity"]["info"] = "Unrelated Ultra receiver"
        with self.assertRaisesRegex(ValueError, "tinySA"):
            self.check()
