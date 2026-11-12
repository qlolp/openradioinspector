# POSIX release command ownership

The release runner uses a private session anchor for each Linux or Darwin
`run_command`. This covers a command that changes its process group while
remaining in its original session, including Foundation-style child launches.
A command that exits successfully while leaving a live session member fails
the release check and triggers owned cleanup.

The anchor stays alive and unreaped until the actual command, every live
member of its session, and explicitly registered nested owners have closed.
The actual command runs in a separate process group through `posix_spawnp`;
there is no threaded `preexec_fn`. Its stdin remains EOF, stdout/stderr use the
existing log, and inherited ignored or blocked shutdown signals are reset.
The actual child's exit code, including a negative POSIX signal exit, is
recorded separately from the anchor's exit code.

Ownership uses only numeric PID, PPID, PGID, SID, process start identity and
live/zombie state. Linux obtains these fields from `/proc/PID/stat`, ignores
the opaque comm field, and uses a checked pidfd for signaling. Darwin obtains
numeric fields through libproc and pins each target group with a private
keeper before using `killpg`; it has no PID-kill fallback. A reused PID,
unavailable membership, invalid record or
failed cleanup is an error; none grants authority over another session.
Zombies have no live I/O and are excluded from the live-member set. The anchor
is never a cleanup signal target.

Darwin creates a keeper from inside the live held anchor SID. The kernel must
allow it to join the target PGID, then its private readiness acknowledgment,
PID/start, PPID, PGID and SID must all match before a group signal. A target
that exits before the join cannot redirect signaling into a foreign session:
the kernel rejects that join. The anchor's own PGID is refused. A child that
joins the anchor PGID therefore fails closed on Darwin rather than allowing
cleanup to signal the anchor itself.

The keeper blocks TERM/INT and remains alive through graceful group signaling.
An authorized KILL also kills its keeper; that child stays unreaped until all
actual and nested members have closed. An unknown or unexpectedly exited keeper
never authorizes a signal. Another signal after a known KILL requires a fresh
ready/live keeper joined inside the same held SID. Keepers use an isolated
Python startup without user/site hooks and only a private EOF pipe. Their
readiness and joins share the existing cleanup deadline.

Private scope directories have mode 0700, records have mode 0600 and a 4096-byte
limit. Records contain numeric identity and private control/registry links,
without command arguments or environment contents. Each scope lives under the
system `/tmp`, independently of the runner's disposable HOME and TMPDIR.
Nested registries contain validated links to those scopes. Successful cleanup
removes its links and scope; failure retains the original Popen/stdin handle
in the live parent and leaves `recovery.json` and `active.session.json` on disk.
The cleanup error reports the exact recovery path, which survives removal of
the caller's temporary directory.

Each registry record-path pass is collected within the existing registry bound
before any owner is yielded for cleanup. Releasing that owner may remove its
registration directory; discovery must not resume a Python3.10 directory walk
inside the retired directory. Generation, record and numeric ownership checks
still run before cleanup. A missing authority record or a foreign generation
remains an error; the traversal change does not ignore missing live evidence.

For an explicitly identified failed run, recovery can retry only that scope:

```python
from pathlib import Path
from tests.release_process_ownership import OwnedCommand

# Use the exact directory printed in that run's recovery diagnostic.
OwnedCommand(Path("/tmp/ori-release-owner-EXACT-RUN")).stop(15)
```

This repeats numeric ownership checks and retains the scope again if closure
cannot be proven. Terminal released proof is idempotent, including recovery
after the original parent has died or the old anchor PID has been reused.
Recovery does not search process names, environments, ports or unrelated
temporary directories. Existing command and shutdown deadlines are preserved.

This is release supervision, not a sandbox for arbitrary processes that create
new sessions. Windows keeps its existing Job Object behavior. System build
services outside the owned session and explicit nested registrations remain
outside the runner's authority.

Synthetic private control sockets can report Windows WSAECONNRESET (10054)
after abrupt owned cleanup. A fixture accepts that specific Windows result only
after the original managed process confirms closure, or an observed original
POSIX release/empty Windows Job close and controller JOIN establish closure.
Unknown accounting, live/unproved ownership, timeouts, other errors and received
data still fail. This does not normalize HTTP or other application responses.
Synthetic stdout readiness/capture producers write binary LF bytes, so strict
wire comparisons stay unchanged even with Windows text newline translation.

The focused regressions use synthetic children and a private socket lifeline.
They verify timeout, KeyboardInterrupt and a zero-exit parent with a changed-PGID
child while an unrelated sibling session remains alive. Numeric models cover
PID reuse, unavailable evidence and Linux/Darwin identity adapters. Additional
checks cover bounded private records, nested recovery, failed cleanup surviving
caller directory deletion, stdin EOF, signal inheritance and actual negative
exit status. The group-keeper mechanism is additionally exercised against the
real Linux kernel: a foreign-session join is rejected, a TERM-ignoring group is
killed, and its keeper is joined only after closure while an unrelated sibling
remains alive. Portable models check Darwin routing and missing, exited,
reused, misplaced or unready keepers. Linux production still uses pidfd.
Darwin live behavior requires the macOS release run. No physical receiver or
measurement archive is used by these tests.

Linux verification of the hardened implementation on 2026-11-11 passed all
1,887 release tests with zero skips, the 10,045-sweep endurance run, and
unchanged logical dumps for all seven temporary HTTP fixture archives. The
final focused ownership run passed 30 tests. This does not claim macOS or
hardware execution.
