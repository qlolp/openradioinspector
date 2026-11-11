# Canonical Rust archive

`ori-archive` is a standalone Rust 2024 crate for the existing canonical SQLite archive. The service owns device actors, guard/lease/domain decisions and the cross-process data-directory writer lease. This crate owns SQL transactions, validation, read budgets, capacity enforcement and recovery files. It has no hardware access or runtime Python dependency.

## Compatibility and precision

Main schema 6, monitoring schema 4 and `instrument_artifacts` retain their actual Python table/JSON formats. Fixed DDL was extracted from an empty foundation `SQLiteSessionStore` by the owned synthetic fixture generator. A checked-in UTF-16 v5 fixture includes offset timestamps, non-BMP IDs, history metadata and closed WAV bytes. Migration tests compare every original raw and metadata row, including original JSON strings. The v5 ingestion backfill follows Python `ORDER BY id` capture order. A current v6/monitoring4 open does not scan raw history to rebuild the index. Older migration work runs before acquisition starts.

Exact dependency pins and the lockfile resolve rusqlite 0.41.1 / libsqlite3-sys 0.39.1, whose bundled engine reports **SQLite 3.54.1** in the runtime test. Every canonical writer enables WAL, `synchronous=FULL` and foreign keys. UTC output retains six fractional digits; historical timestamp text is not rewritten. Connection-local `context_utc` and `context_utf8` functions provide UTC microsecond and exact UTF-8 byte ordering in UTF-16 archives. No custom-function index changes historical tables.

SQLite REAL can normalize negative zero. New non-null observation levels therefore commit their original eight little-endian IEEE754 bytes atomically with the source and its event:

```sql
CREATE TABLE IF NOT EXISTS ori_archive_schema(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);
INSERT OR IGNORE INTO ori_archive_schema VALUES(1,1);
CREATE TABLE IF NOT EXISTS ori_observation_level_bits(
  observation_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
  level_bits_le BLOB NOT NULL CHECK(length(level_bits_le)=8));
```

Both tables must be absent together or present together, with exactly `(1,1)` metadata. Incomplete/future extensions and unknown main/monitoring versions fail before migrations or WAL changes. No historical supplement is fabricated. Readers validate a supplement's length, finite value and equality to stored REAL, then use the original bits. Old rows fall back to their actual REAL and cannot recover a lost sign. These bytes are canonical raw data: recovery/portable exports preserve them. An older core that does not read/write this extension cannot promise the new observation precision guarantee.

All JSON columns are decoded through `parse_json_value(bytes, max_bytes)`, which reconstructs objects without serde's private-number `ValueVisitor` alias. Literal private Number/RawValue keys, prefix-like keys, arbitrary-size integer tokens and finite float64 values survive archive reopen. Duplicate keys, nonfinite float tokens, excessive nesting and byte-budget excess fail. Service ingress should use the generated strict contract codec as well; ordinary `serde_json::from_str::<Value>` can reinterpret reserved-key metadata under `arbitrary_precision`.

Integer token `-0` is decoded as integer zero, matching Python and the shared contract codec. Explicit float `-0.0` retains its IEEE754 sign. This changes only decoded values; historical JSON column text is never rewritten.

## Public integration surface

Create `Archive::open(ArchiveConfig::new(path))` only after acquiring the runtime writer lease and before starting acquisition. It is a writable migration/open, **not a shadow/read-only entry point**. There is currently no public read-only `Archive` constructor; a shadow service must explicitly refuse these operations until a separate validated read-only facade is integrated. An in-process canonical-path guard prevents a second `Archive` writer, while the service's cross-process writer lease remains required.

All async methods dispatch blocking SQLite work to dedicated threads. `create_session`, `save_session`, `append_sweeps`, `append_observations`, `put_monitoring`, artifact/reference/result helpers and `apply(Vec<Mutation>)` return `CommittedBatch` only after COMMIT. Fixed `Mutation` variants compose related graph changes in one transaction; no caller supplies SQL. Source IDs are insert-only. Raw batches contain 1–128 records, a composition contains at most 256 mutations, and all raw fields validate before admission. Foreign keys, cross-session/stream ownership, duplicate IDs, SQL errors and capacity failures roll back sources, events, precision rows and ingestion cursors together.

`CommittedBatch.items` contains committed values. `first_sequence`, `last_sequence` and `ingestion_boundary` describe durable cursors. `events` is a bounded prefix (256 events and configured response bytes); **always inspect `events_complete`**. If false, consume the complete range from `events(after, limit)`. `subscribe_commits()` broadcasts small post-commit ranges, never raw frames. Its 64-range ring can lag; a subscriber must recover from its durable event cursor. A dropped async caller does not cancel an already admitted transaction: its outcome remains discoverable in the canonical ledger.

`stop_session(value, at)` checks a running session and atomically saves stopped state, emits `session.stopped`, cancels every queued/tuning/listening/decoding investigation, queues its candidate and emits each `investigation.cancelled`. It iterates bounded chunks inside the transaction, so hundreds of open jobs do not require an unbounded mutation vector or ACK. `runtime_reset_mutations()`/`reset_runtime_stream_states()` and `list_open_investigations()` let the service implement startup interruption policy without replaying queued jobs automatically.

Bounded reads include `get_session`, `list_sessions` (ascending UTC creation / exact UTF-8 ID cursor), `get_sweep`, `get_observation`, `sweep_page`, chronological recent `list_sweeps`/`list_observations`, `sweep_context` (at most 16 neighbours each side), ingestion boundary/pages, ownership batches, monitoring membership/records, candidate/investigation/result/reference catalogs, scoped artifact metadata/bytes, capacity, registry counts and analytics summary. `MeasurementCursor { started_at, id }` is an internal seek cursor, wrapped opaquely by HTTP. Pages return `Page { items, next_cursor }`. Normal history retains the legacy inclusive end filter.

`sweep_summaries(SweepQuery, boundary)` is the new summary path: limit 1–100, required session, optional stream, half-open UTC start/end, inclusive frequency intersection, exact descending UTC/UTF-8 ID cursor and fixed ingestion watermark. The service freezes filters plus `sweep_ingestion_boundary()` in its cursor. It selects at most 101 IDs through the normalized-time index and decodes one source at a time. Summaries match Python fields (`point_count`, complete frequency range, first original peak on ties, interval/unit/IDs); arrays/settings are excluded and no source decimation or rewriting occurs.

## Bounds and recovery

Defaults: writer channel 64 operations, retained writer bytes 32 MiB, individual batch 16 MiB, two independent read-only connections, reader channel 32, response 16 MiB, read SQL deadline 2 s, busy timeout 5 s and snapshot-copy timeout 30 s. Limits are configurable and validated; readers cannot exceed two. Queue count or bytes exhaustion returns explicit `QueueFull`, never silent drop. Byte reservations include in-flight transactions and retained ACKs until released. Admission streams its serialized estimate without a second JSON buffer; closed artifact payloads count binary bytes directly. These budgets bound queue payloads, not total process RSS, and callers must also bound HTTP/framing buffers.

Each admitted batch starts its transaction immediately (zero batching wait, within the 25 ms maximum). Readers use separate WAL snapshots and progress-handler deadlines, never the writer's lock. Read replies also have incremental byte budgets. Artifact length is checked before loading its bytes; its SHA-256 is validated. Logical SQLite page count × page size is checked before and after measurement/artifact transactions. A full archive rejects the entire batch without deleting history; lifecycle/disarm metadata remains writable. `capacity()`, `status()`, `queue_status()` and checkpoint results expose actual state.

`backup(new_directory)` creates compatible private recovery format 1: sealed `archive.sqlite`, artifact hash ledger `artifacts.ndjson`, and a manifest published last with sizes, SHA-256, counts and event watermark. SQLite's backup API copies a consistent live WAL snapshot in small steps. The live read snapshot is released immediately after copying; hashing, checks and packaging use the sealed copy. Backup/export concurrency is one and consumes one read worker. Copy/I/O is throttled to a 20 MiB/s accounting budget. Files are flushed, and Unix permissions are private. The snapshot-copy timeout bounds the live snapshot; later sealed-file verification can take longer.

`verify_backup(directory)` checks fixed regular-file names, sidecar absence, checksums, SQLite/FK integrity, schema/precision consistency, artifact payload hashes, counts and watermark. `restore_backup(directory, new_database)` verifies before exclusive publication, rejects existing targets and stale WAL/SHM/journal files, and never overwrites. It requires same-filesystem hard-link support for atomic no-overwrite publication. The caller invokes it with the service stopped and its writer lease held. Verification/export are blocking file operations when called as standalone functions; run them outside async executor threads.

`export_portable(new_file)` preserves raw measurements, original audio and canonical precision, removes webhook credentials/notifications, disables project autostart/resume/preparation consent, marks active analysis interrupted, and VACUUMs the sealed copy to remove deleted secret payloads from free pages. Private recovery deliberately retains recovery settings. Neither format creates external audio paths or follows filenames from a manifest.

## Verification and limits

From `crates/ori-archive`, with the repository's Rust 1.99.1 toolchain:

```sh
CARGO_BUILD_JOBS=2 cargo fmt --check
CARGO_BUILD_JOBS=2 cargo clippy --locked --all-targets -- -D warnings
CARGO_BUILD_JOBS=2 cargo test --locked -- --test-threads=2
CARGO_BUILD_JOBS=2 cargo test --locked --test history_scale indexed_million -- --test-threads=1
```

Focused real temporary SQLite tests cover original f64 bits/IDs/units/time, reserved JSON metadata, historical-row preservation, schema fail-closed behavior, exact UTF-16 ordering, atomic duplicate/FK/capacity/trigger rollback, concurrent batches and notices after commit, explicit overload, concurrent readers during a blocked writer, bounded 300-job stop, 12 MiB binary queue admission, exact WAV/hash, live-WAL backups, concurrent snapshot writes, corrupt recovery rejection, stale sidecar rejection, and portable secret/autostart removal. The default indexed-history fixture contains 10,000 sources; the explicit scale test creates 1,000,000 synthetic two-point sources and checks index selection and bounded summary pagination. This is a query/compatibility test, not a 4096-point throughput benchmark.

Verified 2026-11-11: the initial archive suite and two additional close-completion regressions passed. The separately invoked one-million-source test also passed (21.10 s including fixture creation/open/query checks). `cargo fmt --check` and strict all-target clippy passed. The integrated workspace runs the million-source query test by default too, so its release gate has no ignored cases. The 290-point performance target remains a separate benchmark.

All verification uses synthetic data on this host. It does not prove Raspberry Pi performance, physical acquisition/audio wiring, abrupt power-loss survival or filesystem/hardware durability beyond SQLite FULL and the tested software commit/rollback behavior. The service must recover domain state, own devices and the cross-process lease, serialize HTTP JSON losslessly, and resynchronize lagged SSE consumers from durable cursors.

Closure completion belongs to the owned drain job, so cancellation of an async caller cannot orphan the close state. A failed cleanup is latched: subsequent close calls return `WorkerStopped` rather than a false success. The service must retain its external writer lease after any such failure. Focused tests cover a synthetic failed cleanup join and a cancelled close caller while an admitted write waits on a real SQLite lock.
