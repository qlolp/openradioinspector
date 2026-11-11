# Python archive writer ownership

Every new persistent Python server must participate in the same cooperative
writer lease as the Rust runtime, including a plain unsupervised legacy launch.
`LocalCore`'s historical `core.lock` alone does not exclude a Rust writer.
`archive_writer_owner(session_database)` provides this admission without runtime
credentials, HTTP control, archive reads, SQLite connections or hardware access.

The factory returns `None` for `None` and the exact `:memory:` name. Otherwise it
expands `~`, makes the file path absolute, validates the original path without
following symlink aliases, and eagerly acquires:

```
<absolute database parent>/.ori-runtime/writer.lock
```

This is the same path and OS lock used by `RuntimeBridge` and Rust's
`WriterLease`: Unix `flock`, or a Windows byte-range lock overlapping fs2's
whole-file `LockFileEx` range. A competing writer fails with the shared
`RuntimeBridgeError(409, 'runtime_writer_busy')` before SQLite opens. The lock
file stays in place after release; it must not be unlinked to recover ownership.

## Server wiring

Only call this factory when the supervised `RuntimeBridge` is absent and the
server itself owns creation of `session_database`. An externally supplied
`SessionService` keeps its existing caller-owned lifetime and admission policy.
Do not acquire this lease again when a bridge already owns it.

```python
archive_owner = None
with ExitStack() as cleanup:
    bridge = RuntimeBridge.from_environment(session_database)
    if bridge is not None:
        cleanup.enter_context(bridge)  # FIRST: releases LAST
    elif sessions is None:
        archive_owner = archive_writer_owner(session_database)
        if archive_owner is not None:
            cleanup.enter_context(archive_owner)  # FIRST, before SQLite
            session_database = str(archive_owner.database_path)
    # Create/register archive and worker cleanup only after admission.
    # The owner exits after successful closure of all those resources.
```

Use the returned `database_path` for SQLite as well as for admission: the older
store expands its directory creation path but passes the original string to
`sqlite3.connect`. Using one expanded absolute path avoids a tilde/relative input
protecting a different literal filename. `data_directory` exposes that path's
parent. Attach the independent owner as `httpd.archive_owner` so a caller can
retain and safely retry cleanup after a failed server close. This object is not
a RuntimeBridge and exposes no readiness, authentication or control endpoints.

## Failure and path rules

Successful `__exit__` means the caller's later-registered archive/worker cleanup
finished; only then does the owner release its lease. Any exception escaping
cleanup retains the owner in a strong process-global registry, even if the
ExitStack and server references are garbage collected. `close()` refuses to
undo a previous failure. After proving that the archive and every writer have
actually closed, the caller may invoke the idempotent
`release_after_archive_closed()` explicitly. This confirmation is a caller
responsibility; the owner does not inspect SQLite to infer it. Reentering a
released owner is refused.

The database and existing lock file must be regular, single-link files; symlink
aliases and Windows reparse/junction aliases are rejected. POSIX data directories
and database files must belong to the current user and must not be writable by
group/others. Existing owned `0755` data directories and `0644` database files
are permitted and never chmodded. Other-user-owned directory ancestors are
refused; system-owned non-writable ancestors are trusted. Sticky system temp
directories can contain newly created private children, but cannot themselves
serve as a public writable archive data directory.

Missing parent components are created individually with requested mode `0700`
and checked before proceeding, rather than using `parents=True` with permissive
intermediate defaults. The shared helper requests `0700` runtime state and `0600` lock files and
enforces owner-only state/lock permissions on POSIX. Restrictive process umasks may cause admission to
fail closed; the factory never changes the process umask or existing permissions.
Windows retains the existing helper's loopback lease policy; these tests do not
establish an owner-only DACL or LAN security guarantee.

This is cooperative exclusion among participating cores. It does not block an
unmodified program that ignores the lease, nor replace filesystem protection or
archive/resource shutdown ordering. It never authorizes Rust default promotion
or automatically replays pending work.

## Verification

```sh
python -m unittest tests.test_writer_ownership tests.test_runtime_bridge -v
```

The tests use owned temporary paths, a real SQLite connection for cleanup order,
weak references plus garbage collection for failed-cleanup retention, and actual
independent processes. A stdlib reference process uses fs2's exact Unix/Windows
lock operation and range to test exclusion in both directions. It also checks
noninheritance, canonical relative/tilde paths, private parent creation, existing
permissions, unsafe aliases and refusal before database creation.

Linux verification additionally linked a tiny probe against the already-cached
fs2 0.4.3 library and confirmed actual Rust/Python exclusion in both directions
without creating a database. Windows `LockFileEx` coverage is implemented in the
portable test and remains an explicit Windows execution gate. No physical device
or full release suite is used by this focused slice.
