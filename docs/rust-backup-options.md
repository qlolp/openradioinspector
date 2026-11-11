# Explicit recovery backup budgets

This delivery slice provides the library and its recorded synthetic fixtures. Core/HTTP integration is delivered separately in stage 13; the integration interfaces below do not advertise an enabled Rust service.

Raw HTTP archive export uses `SnapshotExport::open_live`; it does not need a
full recovery copy. Recovery backup is a separate, explicitly admitted operation.
`Archive::backup_with_options(destination, BackupOptions)` adds per-operation
policy without changing `backup()` or `export_portable()` defaults.

`Archive::backup_with_io_budget(destination, options, Arc<dyn BackupIoBudget>)`
adds a shared logical I/O admission callback without adding fields to the
options. It retains that budget through actual SQL work and cleanup, even if
the waiting future is dropped. The [shared I/O hook contract](rust-background-io-hooks.md)
describes chunk accounting, cancellation and caller responsibilities.

```rust,ignore
BackupOptions {
    max_snapshot_bytes: admitted_database_bytes,
    copy_timeout: explicit_copy_duration,
    total_timeout: explicit_total_duration,
    cancel: BackupCancellation::default(),
}
```

The options have no default. The quota is the logical SQLite snapshot size,
including free pages and event payloads, not just selected measurements. Artifact
indexes/manifests are additional files; the caller must reserve and account for
the destination filesystem separately. A zero quota/deadline, copy time beyond
total time, or an unrepresentable monotonic deadline is invalid. Copy and total
deadlines begin when the operation is admitted, including its archive-reader
queue wait. They are never enlarged automatically.

The same frozen SQLite transaction supplies page count/size and the backup.
Before creating destination parents or staging files, admission checks the
source quota and the minimum read-plus-write copy time at the existing logical
20 MiB/s rate. Each copied byte costs two accounted bytes. A 30-second copy can
therefore handle at most about 300 MiB before overhead; a larger explicit quota
does not override the explicit deadline. Quota/deadline failures are
`QueryBudget`; invalid policy is `Invalid`; explicit cancellation is `Closed`.
The verified manifest must also fit `ArchiveConfig::max_response_bytes` before
publication; an insufficient response budget returns `QueryBudget` and removes
the owned stage without publishing a bundle.

`BackupCancellation` is cloneable. `cancel()` is permanent and shared with the
admitted operation; `Default` creates a fresh uncancelled token. `BackupOptions`
is cloneable and requires all four fields. The caller must retain and await the
owned backup task (for example, its Tokio `JoinHandle`) after cancellation.
Dropping the waiting future does not detach file I/O or free the single shared
backup slot. SQLite steps, hash blocks and throttle waits
are limited to 512 KiB, 1 MiB and 10 ms respectively; cancellation/deadlines are
rechecked before publication. Deadlines are cooperative: an operating-system
file operation or sync must return before its owner can finish cleanup. The total
deadline also covers sealed-file validation, artifact hashing and packaging.
Copy completion does not acknowledge a recovery bundle until verification and
publication finish. An owned-stage/publication cleanup failure is sticky in
Archive, and repeated close cannot acknowledge it as successful cleanup.

Legacy `backup()` keeps its configured 30-second default snapshot-copy deadline,
with no newly imposed total deadline or source quota. Its later sealed-file
verification may take longer. The limited API reuses that production format,
precision validation and file verification; no source rows or historical bytes
are rewritten. Integrity hashes detect changes; they are not signatures.

Core owns paths, authentication, global research/QoS admission, cancellation and
filesystem capacity policy. Client JSON cannot choose a filesystem destination
or silently increase resource limits. Cooperative logical accounting does not
claim physical disk bandwidth, filesystem quotas or power-loss acceptance.
Whole-database research defaults remain 256 MiB of source and the existing
10 GiB cache/temp profile. A larger operator-selected recovery quota is not
evidence that those reference limits passed.

The bounded footprint proof used a new nonarmed synthetic node and an immutable
Core executable: 1,000 original 290-point sweeps and their 1,000 source events
occupied 12,738,560 SQLite bytes, including a 286,720-byte empty baseline.
Linear extrapolation is about 11.6 GiB for one million sources before scale
margin. This is a capacity estimate, not a million-source, RSS, latency, Pi,
physical-device or physical-storage acceptance result. No retained archive was
copied or deleted for that measurement.

Reproduce the software checks from the repository root with Rust 1.99.1:

```sh
CARGO_BUILD_JOBS=1 RUST_TEST_THREADS=2 cargo test -p ori-archive --locked --offline
cargo fmt --all -- --check
CARGO_BUILD_JOBS=1 cargo clippy -p ori-archive --all-features --all-targets --locked --offline -- -D warnings
```

Verified on 2026-11-11: all 94 archive tests passed with no failed, ignored or
filtered tests; formatting and strict Clippy passed. Eleven added tests cover
explicit policy and legacy compatibility, cancellation ownership, source quota
and copy-floor rejection before staging, reader queue deadlines and overflow,
final-step deadline expiry, changed sealed-copy rejection, prepublication
response limits and sticky cleanup failure. The overflow and response-limit
regressions each failed before their fixes. These are synthetic temporary-file
software checks. The existing million-source query fixture uses two-point
sources and does not establish million-source recovery-copy performance or the
290-point acquisition acceptance target.
