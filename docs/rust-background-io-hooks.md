# Shared background I/O accounting hooks

This delivery slice provides the library and its recorded synthetic fixtures. Core/HTTP integration is delivered separately in stage 13; the integration interfaces below do not advertise an enabled Rust service.

The optional hooks let Core apply one admitted logical I/O budget to legacy
snapshot preparation and recovery backup work. Archive and Legacy do not depend
on Runtime. Their existing constructors, options, format and default pacing
remain available without a hook.

The additive interfaces are `BackupIoBudget::account(bytes)`,
`Archive::backup_with_io_budget(destination, options, budget)`,
`LegacyIoBudget::account(bytes, cancel)` and the default-`None`
`LegacySciencePermit::io_budget()`. Budget implementations must be `Send + Sync`.
They may block cooperatively and must return when their cancellation or deadline
policy requires it; a caller cannot preempt an arbitrary blocking callback.

The library checks its own cancellation and absolute deadline before and after
each callback, including a callback returning an error. Logical source reads and
destination writes are separate charges, before each bounded SQLite-copy step.
Hash reads, sidecar chunks, index/manifest writes and validation scans also use
bounded charges. Existing local pacing remains an additional upper bound.
Logical accounting does not measure physical disk traffic or prove bandwidth.

Hooked SQLite copies charge separate reads and writes of at most 512 KiB per
step. Archive hash/index reads and serialization writes are at most 1 MiB;
legacy sidecar reads and writes are at most 64 KiB. Canonical threshold payloads
use charges of at most 512 KiB. Archive integrity validation precharges its
logical full-file scan in 1 MiB pieces before running SQLite validation. Library
metadata queries and filesystem metadata are not measurements of disk bytes.

The admitted budget is owned by the actual SQL work and its cleanup, rather
than only by the async waiter. Legacy clones it from the original science
reservation before snapshot preparation and retains it after the child exits,
through canonical threshold writes and private-snapshot cleanup. Dropping a
waiting future cannot release the admission while that work remains alive.
Callback failure, cancellation or expiry cannot acknowledge a partial bundle or
publish a legacy cache/version before the corresponding write commits.

Core remains responsible for the shared ledger, trusted worker descriptor,
pressure/cancellation mapping and registration lifetime. These hooks alone do
not enable a Core feature claim. Software verification uses synthetic temporary
files and the existing Python oracle; hardware is not part of these checks.

Run the scoped checks with the pinned Rust toolchain and a Python interpreter
containing the existing NumPy/pySerial dependencies:

```sh
CARGO_BUILD_JOBS=1 RUST_TEST_THREADS=2 cargo test -p ori-archive --locked --offline
ORI_TEST_PYTHON=python CARGO_BUILD_JOBS=1 RUST_TEST_THREADS=2 cargo test -p ori-legacy --locked --offline
cargo fmt --all -- --check
CARGO_BUILD_JOBS=1 cargo clippy -p ori-archive -p ori-legacy --all-features --all-targets --locked --offline -- -D warnings
```

Verified on 2026-11-11 with Rust 1.99.1 and Python 3.12.14: the existing full
Archive and Legacy suites passed, followed by the final seven backup-hook and
six legacy-hook cases; formatting and strict scoped Clippy passed. Denying
callbacks first failed against pass-through implementations before the hooks
were wired. Checks cover separate pre-copy read/write charges, packaging,
deadline/cancellation return, dropped-waiter ownership, private-stage cleanup,
original source hashes, exact IDs/UTC/binary64 data and the unchanged real Python
oracle. These are synthetic software checks.

LegacyRPC's additional child-owned snapshot copy and its shared-budget parser
wiring are outside this slice. Aggregate enforcement remains unadvertised until
Core and the trusted child paths are wired and verified together.
