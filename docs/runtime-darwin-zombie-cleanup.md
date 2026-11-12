# Darwin cleanup of an original exited worker

Runtime retains the original `Child` and its unreaped leader through process-group
cleanup. On macOS, only a group `SIGKILL` error of `EPERM` can enter this additional
proof. `WNOWAIT` must first confirm that the original leader exited. An unavailable
spawn-time birth baseline makes the proof unusable and does not discard the child
guard or change ordinary group cleanup.

The proof makes two bounded original-PGID queries. Each must contain the leader;
zero, full/truncated, misaligned, duplicate or invalid PID replies are rejected.
Every member is queried in **both** snapshots for exact PID, original PGID,
expected effective UID, birth seconds/microseconds and `SZOMB`. Both complete
membership and all returned identities/states must agree. Live or foreign members,
PID reuse, denied/short/missing information and other uncertainty preserve the
original `EPERM` and the existing owned cleanup retry. No enumerated PID is
signalled, no global scan is added and the leader is never reaped to obtain proof.
Only a successful proof calls the same original `Child::wait`; a failed wait is
propagated without marking the child reaped. Other errors and platforms retain
their existing behavior.

The adapter uses the existing pinned `libc::proc_bsdinfo` and libproc declarations,
with compile-time size/field-offset checks. `proc_listpids(PROC_PGRP_ONLY, pgid, ...)`
preserves byte counts; the convenience `proc_listpgrppids` wrapper divides them by
`sizeof(int)` and would hide malformed partial replies. The fixed 256-PID buffer
admits at most 255 complete members. `PROC_PIDTBSDINFO` argument 1 includes zombies.
This uses the macOS SDK's system libproc, with no added crate or SDK installation.

The source rationale is Apple XNU commit
`f6217f891ac0bb64f3d375211650a4c1ff8ca1ea`, `bsd/kern/kern_sig.c`:
explicit process-group iteration excludes `SZOMB`, and no eligible recipient can
produce POSIX `EPERM`. A live permission failure can also produce `EPERM`, so the
error alone never establishes closure. The pinned source is not established as
the exact kernel revision of the hosted macOS 27.0.1 runner.

Pure Rust tests exercise the proof using a fake kernel, including second-snapshot
birth/state changes, malformed replies and failed authoritative wait. A macOS-only
regression spawns literal `/usr/bin/true` without descendants, waits without reaping,
and always finishes cleanup with its original child before assertions, including
the failing baseline. Local Linux checks do not count as a native Darwin result;
the actual macOS regression and the previously failing Core success-worker cases
still require hosted macOS execution. No grace period, workload deadline, cleanup
retry or worker admission limit is changed.
