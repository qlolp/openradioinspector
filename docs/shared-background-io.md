# Shared cooperative background I/O

`BackgroundQos::with_shared_io(private_root)` creates one private POSIX budget
for a core instance. Its async `account` and dedicated-thread
`account_blocking` share a 20 MiB/s logical timeline with trusted Python workers.
The default constructor retains the existing process-local implementation.
This feature does not establish physical I/O, hard filesystem or hardware
performance limits. Windows shared-budget construction fails explicitly.

Core creates its temporary background, TaskBook and Legacy workspace containers
with explicit Unix mode `0700` before starting their file users. A private
ancestor does not replace this leaf-directory policy.

Reserve a background work guard before preparing a source and retain it until
the actual SQL/file owner or child process has joined. Inject `worker_io()` only
into a trusted child envelope's `shared_io` field. This Serialize-only capability
has no Debug representation and must never enter an HTTP request/result, public
log, or user-supplied path field. A configured failed capability remains present;
callers must not replace it with independent local pacing.

Rust uses `clock_gettime(CLOCK_MONOTONIC)` and Python uses
`time.clock_gettime_ns(time.CLOCK_MONOTONIC)`, in the same operating-system clock
domain. Each charge is at most 8 MiB. File-lock admission is nonblocking, polls
cancellation/deadlines every 10 ms and fails after two seconds of contention.
Waiting for a reservation also polls every 10 ms. Cancellation leaves reserved
future bytes charged, so pressure cannot cause a catch-up burst. The fixed rate,
checked integer arithmetic and maximum 30-second future debt reject corrupt or
overflowed state rather than sleeping indefinitely on a supplied timestamp.

The file lives in a newly created 0700 instance directory and has mode 0600,
one link, and a random 128-bit nonce. Both implementations reject symlinks,
wrong owners/modes, changed directory/file identity, incompatible state, wrong
nonce or length. Rust retains its opened inode; Python validates its own opened
descriptor against the capability path. The Python constructor validates state
before creating a cache or reading source data. The existing core package
initializer may import NumPy while loading the stdlib-only shim.

The 128-byte private format is little-endian:

| Offset | Bytes | Value |
| --- | --- | --- |
| 0 | 8 | `ORIIOV1` followed by a zero byte |
| 8 | 16 | Instance nonce |
| 24 | 8 | Fixed bytes/second: 20971520 |
| 32 | 8 | Next reserved POSIX monotonic nanosecond |
| 40 | 8 | Total logically charged bytes, at most signed-int64 maximum |
| 48 | 8 | Creation monotonic nanosecond |
| 56 | 8 | Clock/format code: 1 |
| 64 | 64 | Reserved zero bytes |

`close_shared_io().await` establishes pressure, waits for registered owners to
drain, and awaits its owned cleanup operation. Shared accounting/cleanup faults
are sticky and visible in `cleanup_failed`; successful accounting advertises
`cross_process_logical_io`, while `physical_io_enforced` remains false. Cleanup
verifies ownership and removes only its own file/directory. Replaced paths are
retained and closure returns an error. Core must treat that error as an
unsuccessful shutdown rather than claim completed cleanup.

The stdlib-only `radioinspector.worker_io.SharedIoBudget` supplies the same
capability to isolated workers. Research workers retain their existing local
pacing unless the trusted capability is supplied; shared mode replaces that
independent timeline. Metrics report the mode but never serialize the path or
nonce. Legacy and backup libraries use optional dedicated-thread callbacks;
neither library needs a dependency on Runtime.

Verification uses real private files, two concurrent Rust callers and the
Python worker budget, plus actual isolated research-worker IPC. Tests cover
aggregate bytes/time, cancellation debt, pressure/JOIN, source/cache accounting,
wrong nonce, unsafe modes, corruption, replaced directories/files and retained
guards. This is synthetic Linux software verification. macOS clock/locking CI
and physical device/storage acceptance remain separate checks. Merely creating
the ledger does not establish that every Core path has been bound to it.

The mixed Rust/Python fixture prefers the declared `ORI_RESEARCH_PYTHON`
interpreter, retaining `ORI_CORE_PYTHON` and `python3` as explicit-test/local
fallbacks. Its source path is `research/src`; importing the core package also
requires its NumPy dependency, so an unprepared interpreter on CI's `PATH` is
insufficient. A fresh dependency-free Python prefix tests that distinction.
Failed startup captures at most 16 KiB of stderr and joins/reaps the original
child and its stderr reader before reporting the readiness failure. The
aggregate 6 MiB charge, 20 MiB/s timeline and 280 ms lower bound are unchanged.
