# Archive session lifecycle receipts

This delivery slice provides the library and its recorded synthetic fixtures. Core/HTTP integration is delivered separately in stage 13; the integration interfaces below do not advertise an enabled Rust service.

`ori-archive` provides an opaque receipt for the actual committed lifecycle of a
session. A Core dispatch can check that receipt before its existing execution
boundaries, including after an asynchronous analysis or device operation. The
same canonical writer tracks Session and Guard mutations made through their
shared Archive; an HTTP observer is not required.

Policy authorization, worker ownership, device checks and cancellation remain
the caller's responsibility.

## Public contract

`Archive::capture_session_lifecycle(&str).await` returns a
`Result<SessionLifecycleReceipt>`. It enrolls an existing session using a bounded
command on the canonical writer. Missing IDs return `NotFound`; a closing or
closed Archive returns `Closed`. It does not acquire the domain coordinator and
can be called while the caller already holds that coordinator. The writer queue
orders enrollment after previously admitted mutations, so it does not capture a
stale reader-connection snapshot.

Enrollment is allowed for Draft and other nonrunning states. Such a receipt's
`check()` is false, but retaining it keeps the session's weak registry entry
enrolled. Starting the session can then make a new Running dispatch receipt
available without changing the original enrollment receipt.

`Archive::cached_session_lifecycle(&str)` returns an `Option<SessionLifecycleReceipt>`.
It takes only a short registry lock and performs no SQL. It returns a new
immutable receipt only for an enrolled, current committed Running generation
when no lifecycle commit is pending and the Archive is open. Unenrolled,
nonrunning, pending, poisoned or closed entries return `None`.

`SessionLifecycleReceipt` is `Clone`. Its only public method, `check() -> bool`,
uses atomic state and an Archive lifetime check; it performs no SQL and takes no
registry or domain lock. There is no public constructor, serialized proof,
signature accessor or receipt refresh method. A receipt does not keep the Archive,
its writer thread, SQL connection or database lease alive.

Capture a dispatch receipt once during setup, after checking the actual Session
is Running and before science, evidence publication or device effects. Keep
checking that same original receipt. Calling the factory again after an await
would authorize a different lifecycle and is not a valid continuation of the
original dispatch. Draft policy opt-in can retain its enrollment without trying
to authorize a dispatch; the scheduler can preserve its existing
`session_not_running` result until an explicit start.

## Commit ordering and invalidation

The canonical writer compares actual SQL lifecycle fields before and after each
enrolled Session mutation: exact session ID and existence, `state`, `created_at`,
`started_at` and `ended_at`. It retains only a private SHA-256 fingerprint and a
Running bit. It does not cache measurement payloads or session JSON. Metadata,
labels, stream settings, runtime stream state and ordinary source appends do not
change a lifecycle generation. Another session's lifecycle does not revoke this
session's receipt.

For an actual changed lifecycle, the writer marks only affected atomic cells
pending before SQLite COMMIT. Old checks and the cached factory deny during this
interval. After COMMIT succeeds, it publishes a new immutable generation before
replying or sending commit notices. Thus even an independently readable stop
commit cannot leave the old cache-only receipt valid while publication is
delayed. No global lock is held through COMMIT or through a device operation.

If COMMIT fails, the writer restores the prior atomic generation only after
SQLite reports autocommit and actual SQL lifecycle signatures match all original
affected signatures. An error before COMMIT rolls the transaction back without
publishing a lifecycle generation. Unknown rollback or panic outcomes remain
pending and deny further authorization. Checks may conservatively return false
during a transaction that later rolls back.

Every successful lifecycle transition revokes earlier receipts permanently,
including a stop and start that return identical final values within one
transaction. Changing a session's creation identity and changing it back does
not revive earlier authority. Archive close, final owner drop or writer exit
invalidates all receipts. Opening the same archive again cannot revive a receipt
from the previous instance.

This is a point-in-time check, not an I/O lease: a lifecycle transition can begin
immediately after a successful check. Existing owned cancellation and completion
checks must still surround each effect. All canonical Session writes must use
the Archive's existing writer ownership; direct external SQLite writers are not
a supported source of receipt updates.

## Bounds and verification

The registry contains weak cells and exact, unnormalized ID keys. It permits at
most 1,024 live enrolled sessions and 1 MiB of retained key bytes per Archive.
Enrollment above either limit returns `QueueFull` for `session lifecycle receipts`
without evicting active receipts. The final receipt/cell drop removes its matching
entry, and enrollment also prunes dead entries. Enrollment commands use the
existing bounded writer operation and byte budgets. Generation overflow fails
closed.

Public tests use actual Archive commits for Draft/start/stop, atomic investigation
cancellation, unchanged raw records, unrelated sessions, rollback and restart.
Private deterministic tests pause the writer after a real COMMIT, queue capture
behind a mutation, force a deferred foreign-key COMMIT failure, close with queued
capture, exercise the live-cell quota, and release 1,000 unused enrollments
without retained ID-map growth. These are software tests with synthetic records;
they do not claim hardware acceptance.

The receipt adds no schema, source-record, event or historical-format changes.
