# Owned read-only archive exports

`ori_archive::SnapshotExport::open_live(archive.clone(), query, limits)` opens
one read-only transaction on the Archive's trusted canonical file. It creates
no database copy and accepts no public filesystem path. `BEGIN` and the first
schema/context query freeze one WAL snapshot for the counts, event boundary and
all subsequent records. New writer commits remain possible but never enter
that reader's snapshot. At most two live exports are registered per Archive;
admission beyond that is an explicit `QueueFull`.

The existing `SnapshotExport::open` reads an already completed private backup
without opening another canonical writer. For that mode, reserve the shared
research admission before creating the backup. Create a private `TempDir`, await
`Archive.backup(root.path().join("snapshot"))`, and then transfer that `TempDir`
to `SnapshotExport::open`. An HTTP cancellation must still await the backup
receipt before transferring or removing the directory.

On Unix, both the owned root and its `snapshot` directory must exclude group
and other permissions. Request mode `0700` explicitly when constructing the
root, for example with `tempfile::Builder::permissions`; `tempfile::tempdir()`
uses the process umask and does not guarantee a private directory. The reader
rejects a nonprivate directory and still joins its owned cleanup.

Opening returns immediately after starting an owned thread. `ready().await`
returns session/optional stream, exact sweep/observation counts and the global
event boundary from that same read-only snapshot. A missing session is
`NotFound`; a foreign or missing selected stream is `Invalid`. No HTTP headers
should be sent before this result. In staged mode the backup manifest must be complete and
match the database size/boundary; the caller supplies its own completed backup,
not an untrusted arbitrary database path. `Archive.backup` already verifies the
source checksum and artifacts before publishing that bundle.

`next_record().await` returns one original `Value` with a `Sweep` or
`Observation` kind, or explicit EOF. Sweeps precede observations; within each
kind ordering is canonical UTC microseconds followed by exact UTF-8 ID bytes.
The existing archive decoders preserve integer Hz, original level units,
negative zero and adjacent float64 values. The SQL sort handles only IDs and
spills to disk; the connection has a 4 MiB page cache and no memory mapping.
The mailbox holds one request and record memory is bounded independently of
archive length. The default 16 MiB record and five-second SQL-operation budgets
can only be lowered. A bad or oversized record fails the stream permanently;
it never becomes EOF or a truncated successful export.

The body owner must keep the reader, global admission and background QoS guard
until `close().await` joins its thread. Closing is shared, idempotent and survives
cancellation of its first waiter. A reaper is established before the reader
thread can start I/O. Last Drop only signals it; no reader JOIN blocks an async
executor. The reaper retains completion ownership and the live Archive lease
until actual JOIN. The SQLite connection and cursors close before staged
temporary files are removed. `close().await` is the completion barrier. A
cancelled record read must terminate that HTTP body rather than
retrying the shared cursor: this reader does not offer a client checkpoint.

This primitive does not write raw measurements, own a hardware port, alter
runtime policies, accept SQL or publish an HTTP route by itself. Core integration
owns response encoding, pacing, authentication, revocation and shutdown order.
Archive registers lifecycle Weak references atomically with closing, including
pending starts. A reaper keeps that state reachable after the last client is
dropped. `Archive.close()` refuses new exports, cancels all registered readers
and waits for actual JOIN before draining its normal readers and writer. An
export cleanup failure is sticky in Archive even after its Weak entry expires.

A live read transaction can retain old WAL pages while acquisition continues.
Core must keep its shared research/QoS and logical I/O pacing admission through
close, enforce its explicit operation deadline, watch source/archive/free-space
budgets, and cancel on acquisition pressure or client revocation. This reader
does not raise quotas, delete measurements, checkpoint away a reader's snapshot
or claim physical I/O enforcement. Live export removes the full-database temp
copy; SQLite's ID sort can still use temporary disk space. Whole-database DuckDB
source/cache limits and private recovery backup policy are independent.
Existing nonblank IDs retain their original length and control characters;
they are bound SQL values and never paths or response-header values. Explicit
snapshot removal is part of closing: a removal failure remains sticky and
`close()` fails for every waiter. The body owner must retain its failed-cleanup
ownership/quota accounting rather than treating that failure as released space.
Synthetic regressions cover stable snapshot counts during new commits, exact
raw data/UTF-8 ordering, filtered/empty/missing context, lowered budgets,
malformed manifests, actual SQLite-lock cancellation and stage cleanup.
Live regressions additionally cover new commits/reopen recovery, two-reader
admission, retained client handles at Archive close, registered pending starts,
cancelled close callers, nonblocking last Drop, startup failures before I/O and
sticky cleanup failure after registry expiry. These are bounded synthetic Linux
software checks, not million-source, Pi, physical-storage or hardware acceptance.
