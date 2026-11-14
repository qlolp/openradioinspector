# Original worker receipts

`DeviceManager::capture_worker(&Target, ExecutionGate)` captures one existing,
ready Core-owned worker without opening a port. The opaque cloneable
`RetainedWorkerHandle` exposes `target`, `worker_instance`, `connection_id`, a
cache-only `check`, and the same `execution_gate`. It has no public constructor,
serialization or debug representation. Its operational check retains the
original policy callback and immutable worker/connection identity, rejects
stopping, closed or tainted workers, and never binds a new authorization epoch.

The handle does not pin a tune revision: its own successful tunes remain valid,
while each `TuneOutcome` separately proves the exact post-ACK revision.
`DeviceManager::stop_retained_worker(&handle)` is trusted cleanup authority over
the captured Arc, including after the original policy is revoked. It verifies
the originating manager and waits for actual worker completion and owned
transport drop. It does not look up or stop a replacement by target.

The existing tune waiter now distinguishes queued/not-yet-I/O cancellation from
an actual checked exchange. The first is discarded without stopping unrelated
trusted reads. Revocation during I/O wakes cleanup of the captured original
worker and waits for its actual JOIN before returning denial.

Each worker owns a bounded monitor registry: the existing command queue capacity
plus one executing command, with receiver mode input capped at 64 bytes. Queue
admission and monitor registration share the short close-admission lock. The
caller awaits only a oneshot; dropping that future cannot remove the original
authorization monitor. A retained close attempt on the originating runtime
joins the actual monitor tasks. Native worker completion, owned transport drop
and monitor completion are all required before trusted stop returns or an old
worker is considered finished. A monitor's own native cleanup does not join
itself.

The regression uses actual private AR-DV1 duplex exchanges: successful own tunes,
stale result rejection, original epoch revocation during a silent RF reply,
captured cleanup, replacement, repeated cleanup of the old handle leaving that
replacement untouched, malformed ACK taint, independent analyzer and Core close.
Physical devices, serial ports, RF and host audio remain unrun.

A second controlled case aborts only the waiting tune caller during an entered
silent RF reply, then revokes the original epoch. Before any explicit stop it
observes actual native drop and the monitor's completed JOIN, verifies no stale
`device.tuned` event, and proves later cleanup cannot touch a replacement.
