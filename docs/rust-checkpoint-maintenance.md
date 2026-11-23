# Owned WAL checkpoint maintenance

This change addresses bulk checkpoint work on the canonical ingestion writer.
It preserves SQLite WAL mode, `synchronous=FULL`, the canonical schema and raw
values, and publication only after the source transaction commits.

## Evidence and limits

The original synthetic native syscall control held a real raw-export read
transaction, appended past SQLite's 1,000-frame auto-checkpoint threshold, and
released the final reader. The next one-observation commit made the canonical
writer perform 6,806 main-database writes (27,877,376 bytes). Its no-reader
control performed no main-database writes in the corresponding commit window.
This complete count includes SQLite page 1 at literal offset `0`. The retained
historical preliminary proof reported 6,805 writes (27,873,280 bytes), because
its hexadecimal-offset parser omitted that one 4 KiB write; the original proof
and trace remain unchanged.

The complete retained traces also show ordinary ingestion backfill:

| Control phase | No-reader main-DB writes (bytes) | Held-reader main-DB writes (bytes) |
| --- | ---: | ---: |
| Initial 320 sweeps, including session creation | 11,631 (47,640,576) | 4,649 (19,042,304) |
| Final one-observation commit window | 0 (0) | 6,806 (27,877,376) |
| After its reply, including owned close | 8 (32,768) | 0 (0) |
| Whole named-writer trace | 11,639 (47,673,344) | 11,455 (46,919,680) |

All 4,649 held-reader initial-phase writes preceded the 128-sweep prefill
marker; none occurred during sweeps 128 through 319 while that snapshot was
held. The fixed candidate recorded zero main-database writes on the canonical
writer across both whole controls, with writes on the named maintenance thread.
These counts are cumulative page writes, including repeated and metadata pages.
Tracing changes timing; those timings establish no render-latency acceptance.

The original 30-minute run also dropped 322 earlier offered inputs while
background work overlapped. Their cause remains separate from the 67 late
queue drops around final raw-reader release. Moving checkpoint work alone does
not establish complete delivery or the requested latency/resource acceptance.

## Lifecycle

One Archive-owned executor runs only PASSIVE checkpoints on its own connection.
The ingestion connection's WAL hook only wakes that executor; it never copies
WAL pages. Automatic and close-time checkpoints on the ingestion connection
are disabled only together with this owned maintenance lifecycle. Shadow mode
opens no canonical Archive and starts no maintenance executor.

Notifications are coalesced by a commit generation. A completed pass cannot
erase a later commit whose WAL reset reused the same frame count. Explicit
`Archive::checkpoint()` requests retain their original writer-ordering barrier;
the canonical writer only hands off their bounded receipt, then continues.
There is one active maintenance pass and at most one pending maintenance
request, with no raw payload queue.

A continuous receiver can commit new frames while a PASSIVE checkpoint copies
its snapshot. Waiting a full retry interval after every such pass can prevent
SQLite from reaching the current end of the WAL. On a MacBook Air collecting
AR-DV1 observations at revision `7217e393`, the database file measured
270,323,712 bytes at 62,538 observations. The WAL file measured 7,747,210,952 bytes. The
WAL-index reported 1,859,153 frames already backfilled out of 1,880,391.

When a completed PASSIVE snapshot leaves a small advancing tail, the maintenance
executor can request up to six immediate follow-up passes. Each requests its
own I/O permit. A pinned snapshot, lack of progress or denied admission sends
maintenance back to its normal retry interval. Once SQLite reaches the current
end, subsequent commits can reuse the allocated WAL space. A long-lived read
transaction can postpone reuse. Capacity planning includes the existing WAL
file and the space needed for backups.

On a synthetic Linux stream of 1,440 commits at 120 KiB each, the earlier
executor grew the WAL to 190,599,472 bytes. With the follow-up passes, the
observed peak was 7,280,072 bytes and SQLite reused the WAL 29 times during
acquisition. Both archives returned all 1,440 original payloads byte for byte
after reopening. Storage speed and concurrent readers affect these figures.
The standard archive suite makes a writer commit during a controlled PASSIVE
checkpoint. To repeat the longer measurement on a temporary volume, run
`ORI_WAL_MEASUREMENT_BATCHES=12 cargo test --offline --locked -p ori-archive --lib commit_during_bulk_backfill -- --nocapture`.

The maintenance connection alone selects a uniquely named, non-default VFS
which forwards the existing native SQLite VFS. Its bounded data reads and
writes are charged before forwarding. Cancellation can stop the next bounded
operation. A single underlying OS synchronization or metadata call cannot be
preempted; shutdown retains ownership and waits for its actual completion.
This remains cooperative logical I/O accounting, not an OS physical I/O cap.

`Archive::open` uses a local 20 MiB/s timeline. `open_with_checkpoint_budget`
lets an embedding share its existing background policy. Core supplies that
same cross-process QoS ledger and pressure cancellation without taking a heavy
ResearchPermit. The embedding supplies `CheckpointBudget` admission and an owned
`CheckpointIoPermit`. Busy or acquisition-pressure admission defers a pass.
The permit survives until the native connection has closed. Cancellation asks
that same permit to stop; it does not detach the executor. A real maintenance
failure is sticky and refuses later mutation admission. SQLite BUSY/LOCKED and
a structured PASSIVE `busy:1` result remain retryable, preserving the explicit
caller's original result. No raw data or WAL is deleted to recover space.

A real native cleanup error supersedes retryable cancellation. Later
cancellation cannot replace an already recorded real error.

Archive closure cancels and joins maintenance before its writer lease can be
released. Committed WAL may remain on disk after close; reopening must recover
all acknowledged source IDs, binary64 values, UTC intervals and artifacts.
Core closes this Archive owner before removing the shared background ledger,
and retains its outer writer lease through both operations.

Synthetic coverage includes real WAL reset/frame-count reuse, a real exclusive
SQLite owner and real checkpoint-lock contention, partial-backfill cancellation,
sticky I/O failure, dropped close observers, lease retention and durable reopen.
VFS controls cover short-read zero filling, cancellation before a native write,
native WAL locks, default-VFS isolation and containment of a Rust budget panic.
These checks do not constitute completed latency, resource or hardware
acceptance; concurrent reference verification is separate.
