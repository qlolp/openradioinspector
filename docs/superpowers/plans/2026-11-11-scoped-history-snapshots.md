# Scoped History Snapshots Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan in the current session. No nested agents are authorized.

**Goal:** Run a bounded filtered research job even when the canonical archive exceeds the admitted source budget.

**Architecture:** An additive scoped Archive reader shares the existing live-export admission, frozen transaction and owned JOIN lifecycle. History streams selected original records into a quota-bounded, explicitly derived NDJSON source and seals its checksum, filters and event boundary. The isolated read-only worker retains recovery-bundle support and converts the derived source into the existing Parquet tables.

**Tech Stack:** Rust Archive/History, Tokio, existing SHA-256 and JSON codecs; isolated Python 3.15, NumPy, PyArrow and DuckDB.

**Spec:** This document records the scoped modernization requirement and the frozen interfaces below.

## Global Constraints

- The canonical archive, historical schema, raw records and recovery backup formats remain unchanged.
- One shared ResearchPermit is reserved before source selection and retained through worker exit and source cleanup.
- At most two live readers share Archive's existing admission and close/JOIN barrier.
- Preserve exact UTF-8 IDs/keys, full raw sweep arrays, integer Hz, UTC microseconds, original finite binary64 values and raw units.
- DuckDB: two threads, 512 MiB; worker RSS: 768 MiB; combined owned derivatives: 10 GiB; aggregate logical I/O: 20 MiB/s.
- Acquisition pressure cancels and joins selection/worker work before releasing admission. No raw deletion, instrument I/O or runtime dependency downloads.
- Source budget applies to the selected derived source, including its seal, rather than the entire database.

## Review Focus

- An unrelated oversized session must not prevent a small selected job.
- Coarse frequency overlap must not admit a sweep with no selected point; selected sweeps retain every original point.
- Concurrent writes must not change a frozen selection, boundary, replay cursor or source digest.
- Cancellation and source overflow must remove owned incomplete sources and leave canonical data untouched.
- Derived inputs must not masquerade as full recovery backups or accept modified/linked/unsealed sources.

## Frozen Interfaces

`ScopedSnapshotQuery` has optional `session_id`, `stream_id`, `started_at_us`, `ended_at_us`, `min_frequency_hz` and `max_frequency_hz`. IDs are exact strings; other fields are signed 64-bit integers. Time selects the start instant in a half-open interval; Hz bounds are inclusive.

`ScopedSnapshotExport::open_live(Archive, ScopedSnapshotQuery, SnapshotExportLimits)` returns an owned reader. Its async `ready()` returns `ScopedSnapshotInfo { boundary: u64 }`; `next_record()` returns an optional `ScopedSnapshotRecord { kind: ScopedSnapshotKind, value: Value }`; `close()` acknowledges the existing owned JOIN. Kinds are `Stream`, `Sweep`, `Observation`. Values are canonical JSON; streams include `session_id`. Old export types remain unchanged.

The derived source contains `records.ndjson`, ordered by kind then exact ID bytes, and a final `manifest.json` with `derived_source_version: 1`, `purpose: "derived-history"`, records file/byte count/SHA-256, event watermark, exact filters, stream/sweep/observation counts and `audio_payloads_copied: false`. It has no recovery `backup_version`. A source is published only after quota checks, fsync, reader JOIN and sealing succeed.

## Task 1: Frozen Scoped Reader

**Files:** Add `crates/ori-archive/src/snapshot_export/scoped.rs` and `crates/ori-archive/tests/scoped_snapshot_export.rs`; minimally adapt existing snapshot-export internals and lifecycle.

- [x] Write tests for exact filtered raw records, all-session scope, sparse frequencies, original float bits, concurrent commits, shared two-reader slots and absolute read deadlines.
- [x] Run the new Rust test and confirm missing API RED.
- [x] Implement the frozen additive interface using the same existing lifecycle and Archive registration.
- [x] Run focused tests and all existing Archive/export/backup tests before commit.

## Task 2: Read-only Derived Worker Input

**Files:** Add `research/src/ori_research/derived.py` and `research/tests/test_derived_source.py`; adapt `source.py` and `export.py` without changing existing table schemas.

- [x] Write tests for explicit derived provenance, source/checksum/scope validation, bounded records, NFD metadata and precise Parquet values.
- [x] Compare summary, occupancy and exported data against the same records in a frozen full SQLite source.
- [x] Observe RED, then implement strict immutable NDJSON ingestion and shared table conversion.
- [x] Run the complete isolated research suite; preserve existing full-bundle behavior.

## Task 3: Bounded History Source Selection

**Files:** Add `crates/ori-history/src/snapshot.rs`; adapt `job.rs`, `storage.rs`, `lib.rs` and scoped tests/documentation.

- [x] Write a failing job test where actual canonical bytes exceed a lowered source cap while selected records fit it.
- [x] Add selected-overflow, concurrent-write/replay, source checksum and pressure/cancel/close/restart cleanup cases.
- [x] Replace whole-database admission/backup with selected-source production under the existing permit and BackgroundQos guard.
- [x] Charge bounded selected reads and writes to the shared pacer, check quota before writes, and join the reader and cleanup before returning.
- [x] Run the full History suite, full Research suite, Archive regressions, formatting and strict Clippy; record only synthetic software verification.
