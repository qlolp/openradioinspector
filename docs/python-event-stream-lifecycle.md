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

The parent must run the full available Python release runner on the assembled
candidate before publication. This focused profile is not a passing full-release
gate or a physical/device, macOS or Windows acceptance claim.
