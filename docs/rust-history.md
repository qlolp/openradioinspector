# Rust history and derived analytics

This delivery slice provides the library and its recorded synthetic fixtures. Core/HTTP integration is delivered separately in stage 13; the integration interfaces below do not advertise an enabled Rust service.

`ori-history` adds an owned history service alongside the canonical `Archive`.
It does not own acquisition, stop/disarm a guard, change source measurements,
or install a service. Python remains the accepted default until the assembled
software and physical acceptance gates pass.

Owned snapshot child directories and history storage roots are explicitly
non-recursive on all platforms; the Unix-only `0700` permission request remains
conditional. This preserves the directory-creation policy and keeps non-Unix
builds free of an unused mutable-builder warning.

## Integration and lifetime

Construct `HistoryService::new(archive.clone(), shared_pool.clone(), worker,
config).await`. The `ResearchPool` must be the same instance used by scientific
analysis; create it once per core, never per request. Set `config.qos` to the
same `BackgroundQos` used by other derivative work. The worker configuration
must name an installed isolated Python 3.15 interpreter with exactly
`["-I", "-m", "ori_research"]`. A source checkout/PYTHONPATH is not equivalent.
The scoped-source integration requires worker **0.2.2** or later. Existing
sealed recovery-bundle input remains supported by that worker.

The service supervisor owns the heavy task's join handle. It reserves the
shared `ResearchPermit` **before** selecting source records, then passes that
same permit into the worker. `run_retained` returns the reservation only after
the child and all IPC threads JOIN; History retains it through validation and
retired-source cleanup. A cancelled request cannot detach an admitted job.
`close().await` cancels and joins work, revokes/finishes derivative readers,
and removes owned snapshots before acknowledging closure. Cancelling the
close future does not cancel this shutdown. Dropping the last service client
also signals the supervisor to drain. Keep the executor alive and await close
before closing the canonical archive or releasing the core's writer lease.

Hard child memory enforcement is required unless `WorkerSpec` explicitly
selects best effort. Missing delegated Linux cgroup configuration is refused
by default. Existing runtime enforcement flags are returned unchanged and
describe the **worker**, not the entire core. The current runtime has owned
process-tree support on Unix; unsupported Windows backend execution returns
`WorkerUnavailable`, while existing core/legacy paths remain available. This
does not restrict Windows clients connected to a supported remote core.

## Jobs and exports

`submit(HistoryRequest)` supports only `export`, `summary`, and `occupancy`.
Requests deny unknown fields and accept typed session/stream IDs, integer UTC
microsecond/time and Hz bounds, page size 1–500, and an occupancy threshold in
its original unit. They accept no SQL, commands, URLs, or filesystem paths.
Configuration supplies two separate absolute, private, owned roots; linked
paths, another owner and non-cache existing data are refused.
Each temporary source leaf requests Unix mode `0700` before its ownership marker
or source files are created. Strict leaf ownership and permission checks still
apply even when its configured parent is already private.

Each new job uses `ScopedSnapshotExport` to freeze a read-only transaction on
the canonical archive and stream selected original streams, sweeps and
observations into a bounded derived source. It shares the existing two live
reader slots and Archive close/JOIN barrier. Session/stream predicates and
coarse sweep time/frequency predicates run in SQL; exact frequency selection
retains each matching sweep's complete original arrays. The existing worker
then applies its point filter, preserving original point indices and science
semantics. Start time bounds are half-open and frequency bounds are inclusive.

History seals `records.ndjson` and its distinct manifest only after the
selection reader has JOINED. The manifest freezes filters, counts, SHA-256 and
the global event watermark. This is explicitly a **derived history source**,
not a recovery backup. Python reads only that immutable source; it never opens
the mutable live database. Canonical measurements, schema and full recovery
backups remain unchanged. See [the derived source contract](scoped-history-source.md).

Defaults admit at most 256 MiB of **selected records plus their seal**, 10 GiB
of combined disposable cache/temp/source file bytes, 32 completed job records
and 8 MiB of retained results. A tiny selection remains available when unrelated
canonical data exceeds the source budget. Every selected record and the final
seal must fit before publication; overflow removes the incomplete source and
returns an explicit failure. Metadata loading does not scan historical jobs;
filesystem accounting fails after 8,192 entries or six nested directory levels.
Exhaustion is visible as a job/resource failure and never deletes originals.
Sources retain strong directory/file identities through cleanup. An unknown
replacement is left untouched, cleanup failure is sticky, close fails, and an
active research reservation is quarantined until the core restarts. Idle
retained sources do not occupy global admission between jobs.

`status`, `jobs`, `result`, and idempotent `cancel` expose bounded metadata.
Job IDs are generated 32-character hex values and are validated before mailbox
allocation; the mailbox holds at most 16 commands. A list contains at most
32 completed records and one active job.
Phases are `snapshot`, `running`, `completed`, `cancelled`, and `failed`.
`source_boundary` is the snapshot's **global event watermark**, with its source
SHA-256. Failures expose a redacted code/retry flag, not private paths or worker
stderr. Results omit `cache_path`; manifests contain relative dataset names.
Expired or restarted-service job IDs return `NotFound`.

`replay(job_id, cursor)` starts a new bounded job on the original sealed
snapshot and frozen request. It does not take a fresh watermark or silently
change the query. The existing worker validates cursor scope. Export has no
page cursor. Reading original sources remains the core's existing raw API.

`export_part(job_id, ExportTable)` leases one of `Streams`, `Sweeps`,
`SweepPoints`, or `Observations`. `ExportReader::info()` returns only relative
file name, size and checksum. Reads are capped at 64 KiB, paced, and reserve
the shared research permit through actual I/O completion. `Busy` means no
progress, never EOF; an HTTP body may retry with bounded asynchronous backoff.
Pressure, cancellation, closed or exhausted deadlines must fail the body, so
partial data cannot be reported as a complete export. At most two readers
are leased. Their shared worker-file lock protects against cache mutation;
new history jobs report busy while readers exist. Service close revokes idle
readers and waits for active file reads; callers holding revoked reader
objects cannot keep the roots locked or read after closure.

## Waterfall viewport

`viewport(ViewportQuery)` reads bounded archived sweep pages through existing
archive APIs. A cursor freezes the session's **sweep ingestion boundary** and
query identity; this is distinct from a job's global event watermark. The
cache key includes the service/source instance, boundary, exact query and
cursor. New commits cannot change an older page. A changed query or service
epoch rejects the cursor rather than replacing it with the latest history.

Each returned row is one original sweep. Integer Hz selects at most 1,024
frequency buckets; each bucket retains the maximum level with its original
measurement ID, frequency and point index. Strict greater-than preserves the
first equal maximum, including signed-zero bits. There is no interpolation
or maximum across different sweeps, settings, units or time periods. Rows
are ordered by UTC microseconds and exact UTF-8 ID bytes; cells by frequency.
Queries are capped at 100 source rows, 32,768 potential cells, an 8 MiB source
record and a 2 MiB response. Continuation is explicit; sources are not silently
truncated. Quality flags identify empty/incomplete settings, heterogeneous
profiles, missing frequency buckets and a paged result. Raw data stays intact.

`MAX_VIEWPORT_CURSOR_BYTES` is the existing HTTP transport's 4,096-byte limit
for the **whole serialized JSON cursor**, including field names, UTF-8 ID bytes
and JSON escape expansion. Cursor IDs follow the Archive's nonblank exact-string
rule; the viewport adds no separate 1,024-byte or control-character restriction.
Spaces, distinct NFC/NFD forms and escaped controls remain original ID bytes.
Both fresh and cached pages are checked before they can return an unusable
continuation. If a required cursor exceeds this transport budget, the complete
page fails explicitly with `ResourceLimit` (HTTP 413) before cache publication.
No ID, row or unit is truncated or normalized to make it fit. Oversized incoming
cursors fail with `Invalid` (HTTP 400) before heavy-work admission.

Counting uses a bounded serializer without allocating the cursor JSON first.
The full page retains its separate 2 MiB result limit. The existing GET cursor
query value remains percent-encoded JSON; its worst-case encoded form fits the
Core's 64 KiB request-head policy with the bounded scope parameters. The 64 KiB
POST body policy is unchanged, and this does not introduce a POST viewport route.

## Acquisition pressure and enforcement

`runtime::qos::BackgroundQos` registers cancellation tokens with owned guards.
Keep each guard until its task is **joined**, not merely signalled. The
`pressure(true).await` barrier cancels registered derivative work, blocks new
admission, and waits for every registration to drain. Repeated true requests
share that barrier. Clearing pressure allows new jobs; it does not restart
cancelled jobs. Raw acquisition and guard preflight must never register with
this background gate. There is no client heartbeat or public pressure control.

For cross-process aggregate pacing, construct one `BackgroundQos::with_shared_io`
instance for the core and pass its clone to every cooperative derivative
consumer. History passes its private worker capability only in the trusted
worker envelope. Selected payload reads/writes, worker source/hash/Parquet I/O,
viewport and export reads then use the same 20 MiB/s timeline, in chunks at most
64 KiB. Default QoS retains its historical in-process behavior and cannot
establish an aggregate limit across separate workers. Other backup/Legacy
consumers must also use that same capability or admitted callback before the
core can advertise aggregate coverage. See [shared I/O pacing](shared-background-io.md).

This is cooperative application accounting, not an OS physical I/O or
filesystem quota. Native SQLite predicate/page reads, filesystem metadata and
native Arrow query I/O are not accounted. Worker flags distinguish
actual cgroup enforcement from sampled RSS/logical cache/temp limits. No timeout
was increased and no strict check was disabled for this implementation.

## Verification

Tests use owned temporary archives and the actual installed Python 3.15.1
worker 0.2.2 (NumPy 2.5.4, DuckDB 2.0.0, PyArrow 26.0.1). They exercise real
export/summary/occupancy, immutable replay after new commits, source/RSS resource
failures, global admission and pressure, cancelled close/last-client drop,
reader revocation, root ownership and bounded viewport provenance. Exported
Parquet is decoded in a separate isolated process to check exact UTC microseconds,
integer Hz, raw float64 bits and distinct normalization-equivalent UTF-8 IDs.
They also cover a small selection from a canonical database larger than its
source budget, selected overflow without publication, source replacement,
read-only checksums and acquisition-pressure cancellation during selection.
The research suite compares all four Parquet tables and summary/occupancy
results with the same records in a frozen full SQLite source. The shared
measurement fixture is unchanged.

After root dependency/lock integration, run:

```sh
ORI_RESEARCH_PYTHON=/absolute/path/to/installed-315/bin/python \
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
cargo test -p ori-archive -p ori-history --all-features --locked -j 1 -- --test-threads=1
cargo clippy -p ori-archive -p ori-history -p ori-runtime --all-targets --all-features --locked -j 1 -- -D warnings
cargo fmt --all --check
```

Development used the shared locked workspace and a privately installed wheel;
this source-format change adds no dependencies or lock changes.
This is synthetic Linux software verification; physical Pi/storage/device,
hard-cgroup, Windows backend, macOS runtime and long-duration resource/latency
acceptance remain unrun. Functional checks during other builds do not establish
performance targets.
