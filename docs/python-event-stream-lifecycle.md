# Python event stream shutdown

The Python listener owns its HTTP event subscriptions. The core service owns
measurement workers, guard state and the archive. Closing a client subscription
removes only that subscription; it sends no stop/disarm command.

Both `/api/v1/events` and legacy `/api/stream` register their socket and wakeup
flag with the listener before writing headers. The existing event JSON envelope,
sequence, payload, committed-source ordering and durable cursor replay remain
unchanged. Peer EOF and disconnected/reset/Windows-aborted sockets release the
subscription, including failures while sending response headers.

`server_close()` stops new event subscriptions, wakes existing subscriptions and
shuts down only their owned sockets. It waits for their archive reads, encodes
and writes to exit before closing any owned archive/state resources. A five-second
drain deadline fails closure rather than releasing the archive or writer lease.
After the blocked work exits, the caller may retry this stage safely. Callers
still own the normal `shutdown()`/serving-thread lifetime.

An owned `SessionService` closes after event streams drain. Its raw store close
callback remains registered before construction, so a failed service constructor
still closes SQLite. An injected service remains caller-owned: closing one
listener does not close that service, set its global stream-close flag, or stop
subscriptions belonging to another listener.

The server releases a retained writer lease only after all current resource
closers succeed. This permits a real retry after pending instrument startup or
event-stream work has finished. Global writer/runtime ownership policy is
unchanged. If a callback inside the one-shot resource stack fails, the server
remembers the failure; later `server_close()` or `resource_cleanup.close()` calls
cannot turn an empty stack into a false success. Remaining resources require
explicit verified closure before the owner is explicitly released. The launcher
uses this same ordered server close path instead of closing archive state early.

## Verification

On 2026-11-11, the exact existing pending-startup lease regression reproduced
`runtime_archive_cleanup_not_confirmed` on base `1376719` in a single-CPU Python
profile. New regressions also reproduced premature archive release during a
blocked SSE read and false success on an empty second cleanup pass.

After the fix, 80 related Python tests passed with zero skips in 35.463 seconds
on Linux/Python 3.12. The profile includes writer admission, runtime bridge/server,
launcher, fixture cleanup, durable replay and real owned HTTP stream tests.
The nine new HTTP lifecycle cases cover blocked reads and encodes, a real
constrained slow socket writer, failed drain plus safe retry, failed SQLite close,
constructor cleanup, legacy streams, peer EOF, and reuse of one injected service
across three listeners. Header tests cover broken pipes, resets and simulated
Windows aborted-send errors. The fixtures create only their own temporary
archives and sockets; they open no hardware or existing data directory.

The slow-writer fixture measures actual TCP backpressure before entering its
blocking `wfile.write`. A requested socket buffer size does not prove that a
single 1 MB write will block: all four Windows Python jobs at `7a67925` exited
the write scope before the fixture's unchanged 50 ms assertion. The old scope
recorded neither successful completion nor a filtered disconnect error, so the
hosted logs do not distinguish those outcomes. The fixture now sets
the receiver buffer before connecting, fills the same socket with bounded
nonblocking sends (at most 16 MiB within the existing two-second readiness
bound), and requires observed backpressure. It restores blocking mode, verifies
the real write stays pending, and requires shutdown to end that write with a
disconnect error before archive closure and writer admission. Both small and
larger requested send buffers run on every platform.

Startup regressions separately verify that constructor/bind failure closes the
actual SQLite connection and permits a competing writer, while a real archive
close failure retains ownership. On Linux, both successful-cleanup scenarios
reproduced `runtime_writer_busy` before the fix. The original slow-write fixture
also reproduced its Windows assertion on a real Linux TCP connection with a
larger send buffer; instrumentation confirmed that its single write completed
before the pending-write assertion failed. These are synthetic lifecycle checks,
not device or performance acceptance.

The follow-up passed the full Linux/Python 3.12.14 release runner: 1,850 tests with
zero skips, 10,045 synthetic endurance sweeps, all seven fixture archives
unchanged, and no cleanup errors. Six focused startup/socket cases also passed
on Python 3.15.1. The original command and shutdown deadlines remain unchanged.
Windows hosted execution of this follow-up remains required; local Linux
verification does not establish Windows or macOS acceptance.

The parent must run the full available Python release runner on the assembled
candidate before publication. This focused profile is not a passing full-release
gate or a physical/device, macOS or Windows acceptance claim.
