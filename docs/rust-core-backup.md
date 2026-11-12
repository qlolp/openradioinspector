# Trusted Core embedding backup

`Core::backup(&Arc<Core>, destination, BackupOptions)` is an operator-owned Rust
embedding API. It creates a verified private recovery bundle at an absent
destination using the already-open canonical Archive. It adds no HTTP endpoint,
CLI command, second Archive writer, research reservation or automatic deletion.

All options remain explicit: maximum SQLite snapshot bytes, copy deadline, total
deadline and the original `BackupCancellation`. The Archive's existing single
backup slot and bounded reader admission apply. Invalid quotas, insufficient
copy time, existing destinations and unavailable storage remain errors; Core
does not enlarge limits.

The original Core admission guard and shared background registration live in an
owned operation through the actual Archive cleanup receipt. Dropping the result
waiter requests the same cancellation. Core shutdown, acquisition pressure and
caller cancellation converge on the original tokens; signalling cancellation
does not release the writer lease or background registration. Core closure waits
for real copy/verification/publication cleanup before releasing ownership.

The native backup callback charges the SAME `BackgroundQos::account_blocking`
timeline used by Core's other cooperating background consumers. This is logical
application I/O pacing at 20 MiB/s, including the Archive's explicit copy,
hash/index and verification passes. It is not an OS physical disk bandwidth
limit. Raw exports have their own bounded reader slots; heavy research retains
its independent one-child reservation, so neither is held by this backup API.
Aggregate resource capability remains false where other native/third-party I/O
is not metered.

Synthetic ownership tests use the real Archive WAL/copy budget, held native I/O,
Core writer lease, live raw export and shared QoS. They verify that pressure and
dropped waiters cannot report a completed drain before native cleanup, that live
writer commits continue, and that verified restore preserves original IDs, NFD
metadata, UTC microseconds, integer Hz, binary64 bits and exact WAV bytes. These
checks do not constitute hardware or Raspberry Pi acceptance.

The component gate ran all five backup cases after first observing all five
fail against the unimplemented path. The complete Core unit suite then passed
59 tests with one test thread and the installed Python 3.15 fixture interpreter.
Strict all-target/all-feature Clippy also passed with the verified acquisition
observer consumer integrated. The final assembled workspace still needs its
normal complete runtime gates after all pending modules are combined.

Run the focused checks from the assembled workspace with
`cargo test -p ori-core --lib core_backup::tests -j1`. The tests create and remove
only their own temporary Core data, recovery bundle and restored database; the
application never deletes an operator's existing destination.
