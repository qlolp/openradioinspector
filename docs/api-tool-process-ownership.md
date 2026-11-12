# API-tool process ownership

`probe_cli` and `OwnedNode` use the existing release process authority through a
private adapter. API comparisons, candidate metadata rules, and the public case
inventory are unchanged.

On POSIX the adapter and target run inside an unreaped `OwnedCommand` session.
The target inherits that session; it does not create another session. Cleanup
uses the existing numeric identity checks and Linux pidfds or Darwin group
keepers. Reaping a target or adapter never grants permission to signal its old
PID or process group. On Windows the adapter waits at a private gate until the
existing `WindowsJob` has accepted it; its target then inherits the job. No
target executes after a failed job assignment.

The adapter records the actual target's exit separately from its own completion.
POSIX signal exits remain negative; Windows exit values retain all DWORD bits.
Adapter exit zero requires closed stdout/stderr readers, and the caller also
requires empty owned membership before releasing the session or Job. A zero
target exit and removed readiness file alone cannot establish node JOIN.

CLI stdout and stderr remain separate, with the existing 256 KiB cap for each.
Node output uses one merged stream with one aggregate 256 KiB cap. Writers keep
draining after a cap is reached while discarding excess bytes; overflow always
fails the operation. A native zero exit cannot turn overflow into success.
Supervision records are bounded, nonce-bound files in a fresh private directory.

The adapter flushes the complete bounded stdout/stderr prefix before publishing
an overflow record. The caller may terminate the owned Job as soon as that
record is visible; buffered bytes must already be readable from the retained
log. This preserves the original per-stream or merged aggregate byte limit and
exact bytes, including with a larger platform-default Python file buffer.

Node readiness and shutdown are unchanged: the original target publishes its
owned ephemeral loopback URL, and closing the caller's stdin lifeline publishes
a private stop request that closes target stdin. CLI runtime bounds, the
15-second node readiness bound, and the node's 10-second graceful/5-second forced
cleanup bounds remain in place. Test fixtures reach their actual child/output
readiness before starting an intentionally short timeout; their socket lifelines
release controlled children without signaling PIDs read from files.

An uncertain cleanup keeps its original authority handle, independent directory,
lease, logs, and recovery pointer. POSIX registration also participates in an
existing outer release registry. A caller may retry cleanup through the retained
original authority; it may not delete that lease while closure is unconfirmed.
Windows recovery retains the original Job handle in the parent process. The
kernel's kill-on-close behavior applies when that parent exits; a Job is never
reconstructed from a child PID. Successful closure makes repeated cleanup and
disposal harmless.

Controlled Linux regressions cover changed process groups after leader exit,
inherited pipes, a detached-output descendant behind a zero-exit node, exact
signal status, bounded streams, and late cleanup after caller-directory removal.
Portable Job models cover assignment ordering, native DWORD status, and retained
authority when accounting is unknown. Actual hosted macOS/Windows execution of
this adapter remains a CI verification step. These are synthetic process and
loopback tests, not instrument or performance acceptance.
