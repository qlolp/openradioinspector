# Isolated snapshot research worker

The optional `research/` package runs in its own Python 3.15 environment. The
Python 3.10+ core must never import it or add its DuckDB/PyArrow dependencies.
Only the core owns instruments and canonical archive writes. This child reads
a **sealed, already verified private recovery bundle**, produces regenerable
Parquet, and performs derived summary or occupancy queries. It has no device,
port, SQL execution, network-fetch, source-deletion, or archive-write operation.

Implemented and tested with synthetic SQLite/Parquet/DuckDB data on Linux x86_64:
Python 3.15.1, NumPy 2.5.4, DuckDB 2.0.0, PyArrow 26.0.1. These tests do not
establish Raspberry Pi performance, physical instrument acceptance, or Windows
and macOS runtime acceptance.

Worker 0.2.0 also reads canonical observation precision extension version 1.
The IPC and Parquet column formats remain version 1. The worker version and
extension provenance participate in cache identity, so 0.1.x caches are not
accepted as compatible observation exports.

## Installation and verification

Run from the repository root, using a separate user-owned environment:

```sh
uv venv research/.venv --python 3.15.1
uv pip install --python research/.venv/bin/python --require-hashes --only-binary :all: -r research/requirements.lock
uv pip install --python research/.venv/bin/python --require-hashes -r research/build-requirements.lock
uv pip install --python research/.venv/bin/python --no-deps --no-build-isolation ./research
research/.venv/bin/python -m unittest discover -s research/tests -p 'test_*.py'
```

The interpreter is `research/.venv/Scripts/python.exe` on Windows. Python patch
selection is recorded in `research/.python-version`; `pyproject.toml` permits
the 3.15 series. Runtime packages and the build backend are pinned and hashed
separately. No research test is included in the legacy `tests/` discovery tree.
`research/tests/fixtures/measurement-v1.json` is the verbatim common contract
fixture. Regenerating a lock requires verifying the new artifacts and rerunning
this isolated suite; a version change invalidates cache compatibility.

## Supervisor IPC contract, version 1

Invoke the configured interpreter with an argument list:
`[research_python, "-m", "ori_research"]`, without a shell. Installation is
preferred; a source checkout can instead set `PYTHONPATH` to `research/src`.
Send one UTF-8 JSON object on stdin, close stdin, read one JSON object on stdout,
and wait for exit. Requests are capped at 65,536 bytes; responses at 2 MiB.
JSON duplicate keys, nonfinite numbers, unknown fields, and extra JSON values
are rejected. There is no long-lived worker or stdin command loop.

```json
{
  "version": 1,
  "request_id": "research-000001",
  "operation": "occupancy",
  "snapshot_path": "/srv/ori/snapshots/sealed-73/archive.sqlite",
  "cache_directory": "/srv/ori/research-cache",
  "filters": {
    "session_id": "fixture-сессия",
    "stream_id": "spectrum/one",
    "started_at_us": 1794398400123456,
    "ended_at_us": 1794398400123457,
    "min_frequency_hz": 100000001,
    "max_frequency_hz": 100000004
  },
  "page_size": 100,
  "threshold": {"value": -10.0, "unit": "dBm"}
}
```

`version`, `request_id`, `operation`, `snapshot_path`, and `cache_directory`
are required. `request_id` is nonempty and at most 128 UTF-8 bytes. Paths must
be absolute local filesystem paths. All six filter fields are optional;
omitting `filters` selects the complete snapshot. IDs are bounded strings and
must exist in the source; a stream must belong to the selected session. An
unknown scope returns `invalid_filter`, while an empty time/frequency range
result returns an empty dataset/page.

Time filters select a measurement by its **start instant**, in the half-open
interval `[started_at_us, ended_at_us)`. Bounds are signed int64 UTC epoch
microseconds and must increase. This is not an interval-overlap query.
Frequency bounds are inclusive integer Hz, from 0 through int64 maximum;
booleans and floating point frequencies are invalid. A filtered sweep retains
original point indices and both its original and exported point counts.
No S-meter to dBm conversion, calibration inference, or resampling occurs.

Operations:

| Operation | Additional inputs | Result |
| --- | --- | --- |
| `export` | Optional `limits` | Verified immutable filtered Parquet cache, manifest and manifest SHA-256 |
| `summary` | `page_size` 1–500, default 100; optional `cursor` | Groups by kind/session/stream/original unit; measurement/sample/valid-level counts, time/frequency/level ranges |
| `occupancy` | Required `threshold` with finite numeric `value` and original `unit`; same pagination | Groups additionally by exact frequency; valid sample count, occupied count and sample fraction |

Occupancy counts non-null levels satisfying `level >= threshold.value`, divided
by all non-null levels in the same unit and group. It measures **sample
fraction**, not elapsed-time duty cycle. Sweep points and observations remain
different `kind` values. Groups with no valid levels produce no occupancy row;
summary still reports their measurement/sample counts and null level ranges.

Pagination orders ascending by `(kind, session_id, stream_id, level_unit)` and,
for occupancy, `frequency_hz`. Supply the returned `next_cursor` unchanged with
the same request context and page size; null means complete. Cursors are bounded
base64 JSON, validated against the source/cache, operation, threshold and page
size. They are not credentials and cannot supply SQL.

Successful exit status is 0:

```json
{"version":1,"request_id":"research-000001","operation":"summary","ok":true,
 "result":{"rows":[],"next_cursor":null,"cache_key":"sha256…",
 "event_watermark":73,"cache_hit":true,"cache_rebuilt":false,"manifest_sha256":"sha256…"},
 "metrics":{"peak_rss_bytes":150000000,"elapsed_ms":35}}
```

`export.result` contains `cache_key`, private absolute `cache_path`, `cache_hit`,
`cache_rebuilt`, `manifest`, and `manifest_sha256`. Query results contain the
fields above; occupancy also returns `occupancy_basis: "samples"` and the
threshold. The abbreviated metrics example omits additional accounting and
effective limits. The supervisor should keep paths and manifests private and
expose only the public fields its API needs.

Validation/resource failures exit 2; unexpected internal failures exit 3:

```json
{"version":1,"request_id":"research-000001","ok":false,
 "error":{"code":"cache_busy","message":"Another heavy job owns this cache","retryable":true},
 "metrics":{}}
```

Stable error codes include `invalid_request`, `invalid_filter`, `invalid_cursor`,
`unsupported_schema`, `unsealed_snapshot`, `snapshot_invalid`,
`source_record_invalid`, `source_record_too_large`, `unsafe_cache`, `cache_busy`,
`cache_quota_exceeded`, `temp_quota_exceeded`, `rss_limit_exceeded`,
`duckdb_memory_limit`, `query_failed`, `response_too_large`, and `internal_error`.
An invalid/unreadable envelope may have null `request_id`. Errors do not echo
source settings, SQL, credentials, audio, private configuration, or tracebacks.
stdout is protocol-only. Diagnostic/progress output, if added, belongs on
stderr and must obey the same redaction rule. This version emits no progress
stream. A supervisor deadline or acquisition-priority cancellation terminates
the child/process group; a killed process is not required to return JSON.

## Source provenance and atomic disposable caches

The input must be named `archive.sqlite` beside the recovery
`manifest.json` and `artifacts.ndjson`. The core must first verify the full
bundle with `radioinspector.recovery_backup.verify_backup`, including each
closed artifact checksum. The worker checks the bounded recovery manifest,
SQLite/file and artifact-manifest digests, archive size, schema 6, required
columns, SQLite quick/foreign-key checks, counts, and committed event watermark.
It refuses WAL/SHM/journal sidecars and opens the snapshot with
`mode=ro&immutable=1`, `query_only=ON`, no mmap, and a small SQLite cache. It
checks source identity, size and mtime again before publishing. The core must
keep the sealed bundle immutable and retained for the entire invocation.

The optional canonical supplement uses `ori_archive_schema(singleton, version)`
with exactly `(1, 1)` and `ori_observation_level_bits(observation_id,
level_bits_le)`, where each stored level is the original eight-byte little-endian
float64. Both tables must be present together. Unknown extension versions fail
closed with `unsupported_schema`. Present bits must be finite and numerically
equal to the stored REAL; positive and negative zero compare equal for this
consistency check, while the original bit sign is retained in Parquet. Missing
supplement rows use the historical stored REAL value without guessing a lost
sign or backfilling anything. A legacy archive with neither table also uses its
stored REAL values. The main SQLite `user_version` remains 6.

This supplement is canonical original data. The worker reads it through the
same sealed read-only connection and never creates, changes or deletes its
tables/rows. Derived provenance reports `source.canonical_extensions` as
`{"observation_level_bits":1}` when supported, or an empty object for legacy
sources. The source SHA-256 covers both the ordinary archive and supplement.

Cache identity covers the source SHA-256, full provenance, recovery manifest
SHA-256, filter values, format version, and exact worker/runtime versions. Each
cache contains a final `manifest.json` and four bounded-batch Parquet tables:

| Table | Preserved source context |
| --- | --- |
| `streams` | Source IDs, device/role/unit/label, original settings and capabilities JSON |
| `sweeps` | Source ID/scope, exact UTC microseconds, original unit/settings, source/exported point counts |
| `sweep_points` | Source sweep ID, original point index, scope/time, int64 Hz, original float64 level and unit |
| `observations` | Source ID/scope/time, int64 Hz, original level/unit/mode/squelch/decoded mode/audio URI/metadata |

The original audio URI is a reference; no audio BLOB is queried or duplicated.
Use source IDs and the core API to open original measurements/artifacts. Float64
Parquet preserves negative zero and adjacent representable values. Source
timestamps with precision beyond microseconds are rejected rather than silently
truncated. Derived
aggregates do not promise bitwise identity to raw levels. Original settings
and metadata are stored privately and never included in query pages.

Use a dedicated empty cache directory. The worker creates an ownership marker
and refuses nonempty unowned directories, linked files, special files, and
snapshot/cache path overlap. An OS lock admits one job per cache directory;
the supervisor must admit one heavy job globally. Only owned unfinished stages,
owned stale temporary jobs, and an incompatible/corrupt cache for the requested
key may be removed. No source or arbitrary user files are deleted. Other valid
caches are retained; a full cache returns a quota error rather than evicting
unrelated content automatically.

Parquet writers close and fsync before file hashes and manifest creation.
The source is rechecked before a same-filesystem directory rename publishes
`cache-<sha256>`. No final cache path exists during construction. On failure,
staging is removed; on process kill, the next admitted child removes its
abandoned stage and rebuilds. Cache hits verify every file hash, row count,
schema and context. Corruption triggers a reported rebuild (`cache_rebuilt`),
never silent reuse. Queries register only the two fixed local Arrow scanners;
DuckDB external access and extension auto-install/autoload are disabled.

## Resource enforcement and acquisition priority

An optional `limits` object may **lower**, never raise, these defaults:

| Limit key | Default |
| --- | --- |
| `duckdb_threads` | 2 |
| `duckdb_memory_bytes` | 536870912 (512 MiB) |
| `worker_rss_bytes` | 805306368 (768 MiB) |
| `cache_bytes` | 10737418240 (10 GiB) |
| `temp_bytes` | 10737418240 (10 GiB) |
| `io_bytes_per_second` | 20971520 (20 MiB/s logical accounted I/O) |
| `batch_rows` | 1024 |
| `batch_bytes` | 4194304 (4 MiB) |
| `max_record_bytes` | 8388608 (8 MiB SQLite record limit) |
| `max_points_per_sweep` | 65536 |

The child sets DuckDB's actual thread, memory and spill limits, bounds Arrow
scans/batches and pages, limits SQLite record allocation, admits each Parquet
write against the cache payload-byte quota, and paces accounted source/hash and
Parquet write bytes in 64 KiB chunks. Arrow/BLAS helper threads are restricted.
RSS is checked between bounded operations and monitored during DuckDB queries;
exceeding it interrupts the query or fails at a checkpoint. Metrics report
observed peak RSS, elapsed time, logical byte accounting, maximum Arrow batch
rows/bytes, retained cache bytes, temporary bytes at query completion, and
effective native DuckDB settings. Reads of SQLite native pages and Arrow's
native query I/O are not included in logical byte accounting.

**These cooperative checks are not portable hard RSS, physical I/O, or physical
disk limits.** Native allocation, filesystem metadata, page cache, transient
spill, and process termination can exceed sampled/accounted values. Metrics
explicitly list `hard_limits_require_parent`. On the Pi/Linux service, the
supervisor must use a dedicated cgroup v2 with `memory.max=805306368` and
`memory.swap.max=0`, low CPU/I/O weights and nice/idle I/O priority. Set the
actual SSD device's `io.max` read/write budgets to an aggregate at most 20 MiB/s
(for example, 10 MiB/s read plus 10 MiB/s write). Reserve separate filesystem
project/volume quotas for the cache and its stable `.temporary/` subtree, each
10 GiB. Include all relevant devices and cancel the process group on deadline
or acquisition pressure. `RLIMIT_RSS` on Linux does not enforce this contract;
`RLIMIT_AS` is not a substitute for RSS with Arrow mappings.

Windows should apply an enclosing Job Object memory/process-tree limit and an
appropriate supported I/O rate/storage quota policy. macOS requires service or
container/VM enforcement for equivalent hard limits; sampled termination alone
does not establish them. The supervisor must surface a resource-killed or
cancelled job explicitly, retain the sealed source, and keep acquisition ahead
of analytics. Offline packaging must include the locked wheels and interpreter;
no startup download or runtime package installation is performed by this child.
