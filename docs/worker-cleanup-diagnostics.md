# Owned worker cleanup diagnostics

An unknown research-child cleanup outcome retains the original process owner and
heavy-work reservation. Its first cleanup error offers one record, at most 512
ASCII bytes, to a process-wide best-effort diagnostic dispatcher. The record has
only the original owned PID, Runtime error variant and raw OS error number (or
`none`), never a command, environment, token, error message or child payload.

The owner uses `try_lock` for initialization and `try_send` to a capacity-one
queue. Contention, failed initialization and a full or disconnected queue drop
the record. Initialization starts at most one logger thread per process, with a
requested 128 KiB stack. That thread alone performs direct stderr `Write`, which
bypasses ordinary test print capture. It can hold one in-flight record in addition
to the single queued record; it never owns a child, reservation or archive.

This is a process-lifetime logger: Core's owned-child/IPC JOIN does not claim to
join it, and a blocked diagnostic sink cannot delay the owner's cleanup retry.
Failed writes do not escape cleanup or create more threads. Process-group
authority, actual child/IPC JOIN, retries and deadlines are unchanged. A record
does not prove successful cleanup, and its absence does not locate a pending
operation or establish the cause of a macOS timeout.
