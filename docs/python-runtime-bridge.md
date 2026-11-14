# Legacy Python runtime bridge

`radioinspector.runtime_bridge` implements the private v1 supervisor contract
without creating a listener, importing research dependencies, opening SQLite,
changing historical records, probing hardware, or signaling any process. The
parent HTTP server/launcher must wire it as described below. Without runtime
environment variables the legacy entrypoint remains unchanged.

Verified with temporary SQLite databases, real owned subprocesses and HTTP
servers, real monitoring with explicit simulators, and coordinated threads on
Linux/Python 3.12. No Pi, physical instrument, Windows locking, or deployed
service acceptance is claimed.

## Lifecycle wiring

1. At the beginning of `create_server`, **before** `ApplicationState`, any
   `SQLiteSessionStore`, or hardware ownership, call
   `RuntimeBridge.from_environment(session_database)`. It returns `None` when
   no `ORI_RUNTIME_*` variables are present. Partial/invalid configuration fails
   closed; it never quietly falls back to unsupervised operation.
2. If enabled, `cleanup.enter_context(bridge)` **first** in the construction
   `ExitStack`. Register archive/device/monitoring cleanup afterward so those
   resources close first and the bridge exits last. Do not register
   `bridge.close` as a callback that loses the exception context.
3. Expose the bridge on `httpd.runtime_bridge` and the handler's bound class;
   after the complete server/monitoring construction call
   `bridge.attach(httpd, monitoring)`. Continue to call `monitoring.start()`.
4. In both `serve` and `LocalCore`, close monitoring **before** stopping the
   worker manager, so its close captures opt-in resume intent before worker
   termination can appear as a guard error/stop. Then stop schedulers/devices,
   retain the existing worker-idle checks, close the archive, and exit the bridge.
   On a failure **before** `resources.close()` reaches the bridge, call
   `bridge.__exit__(*sys.exc_info())` in that failure path, then re-raise. This
   latches private strong retention, preventing garbage collection from
   releasing a lease while an archive or worker can remain alive.
5. For the packaged `radioinspector-core` launcher, set
   `core.server.runtime_stop = stop.set` after entering `LocalCore`. The launcher
   blocks on its own stop event; HTTP shutdown alone cannot end that wait.
   The bridge invokes this optional hook and `httpd.shutdown()` from its owned
   background thread. The existing outer context/finally performs resource
   closure. Direct `web.serve()` needs only HTTP shutdown to enter its finally.

Any exception reaching the bridge's ExitStack exit conservatively retains the
lease, including an earlier construction exception: the bridge cannot prove that
external archive cleanup succeeded. `bridge.close()` then refuses to release it. Only
`bridge.release_after_archive_closed()` may confirm a **successful** explicit
cleanup retry. Call it only after archive/instrument writer resources have
actually closed and active mutation handlers have drained. It refuses while
admitted mutations remain. A retained lease otherwise lasts until process exit.
No failed cleanup restores an old database or replays a pending job.

## Environment and lease

The supervisor supplies both `ORI_RUNTIME_CONTROL_TOKEN` (32–256 URL-safe opaque
characters; generated Rust tokens have 256 random bits) and
`ORI_RUNTIME_INSTANCE_ID` (1–128 URL-safe characters). They are never persisted,
logged, included in errors, or returned by the public API. Status returns the
non-secret instance ID and current PID so the supervisor can detect a stale or
wrong child. Tokens must be fresh per launch.

`ORI_RUNTIME_DATA_DIR` selects the existing absolute data directory. Without
that variable, the explicit fallback is the absolute
`Path(session_database).parent`; memory-only startup therefore requires an
explicit runtime data directory. A supplied session database must have exactly
that directory as its parent; nested data owners and hard-linked aliases are refused.
Relative runtime data paths, symlinks, unsafe file types,
public/wrong-owner Unix private state, hard-linked leases, and untrusted writable
ancestors are refused. Unix state is `.ori-runtime/` mode 0700 and
`writer.lock` mode 0600; no raw database is opened by this acquisition.

The open non-inheritable `writer.lock` lease uses nonblocking POSIX `flock`,
matching Rust `fs2`. On Windows stdlib `msvcrt.locking` takes a nonblocking first
byte range; it overlaps `fs2`'s entire-file `LockFileEx` range, including when the
file is empty. Windows private DACL enforcement and platform lock acceptance
remain outside this synthetic Linux verification. No heavy Python dependency or
hashed core dependency change is introduced.

Only participating cores honor this lease. An unsupervised legacy program may
ignore it, and different data directories do not establish different physical
hardware. Keep the supervisor's listener handover checks and each instrument's
OS-level exclusive access. Never delete lock files or kill a process by port.

## Private HTTP wiring

Handle `/internal/runtime/*` before public routes and before the public mutation
gate. For each private request, first call:

```python
bridge.authorize_control(self.client_address,
                         self.headers.get_all('Authorization', []))
```

It accepts only actual loopback IP peers, one exact `Bearer` header, and a
constant-time token match. Do not trust forwarded peer headers, accept tokens in
query strings, log Authorization, or collapse duplicate headers before checking.
An IPv4-mapped loopback peer is supported. Authentication must happen **before**
reading a body, so unauthenticated slow uploads cannot occupy shutdown handling.

For shutdown, use the existing strict bounded body reader with a 4096-byte cap:
require one numeric Content-Length, reject Transfer-Encoding/duplicate lengths,
set the existing whole-body deadline, and reject duplicate JSON fields/nonfinite
numbers. Then call:

```python
result = bridge.control_response(
    self.command, self.path, peer=self.client_address,
    authorization=self.headers.get_all('Authorization', []), payload=payload,
)
```

For GET status omit `payload`. `control_response` authenticates again and returns
`(status_code, json_object)`; non-runtime paths return `None`. Queries and unknown
private paths are rejected without echoing their content. It also accepts at
most 4096 raw UTF-8 JSON bytes for adapters/tests and independently validates
strict shutdown fields. Catch `RuntimeBridgeError` and send its safe `.status`
and `.code`, with no traceback or request echo.

Send Content-Length and a bounded JSON response. For a **202 shutdown response**,
write the response and call `self.wfile.flush()` before
`bridge.after_response_flushed()`. That method is idempotent and starts one owned
background shutdown thread. If writing/flushing that 202 fails, call
`bridge.response_not_flushed()` in the response error path; it reopens admission
only if shutdown has not started. Never call `httpd.shutdown()` from the request or
serve_forever thread itself.

GET `/internal/runtime/v1/status` returns exactly version 1, instance_id, pid,
ready, preflight_complete, guard_armed, and writer_owned. Writer ownership reflects
the open lease. The new `monitoring.startup_recovery_complete` Event starts clear
and is set in `_supervise` only when startup recovery attempts finish; a separate
`startup_recovery_error` latch records a generic recovery failure. Ready and
preflight_complete remain false for incomplete/failed recovery or closed
monitoring. Snapshotting a busy monitoring lock returns a prompt 503 instead of
blocking the supervisor's 500 ms loopback deadline. A cold unconfigured node may become ready to serve UI. This is not
universal hardware readiness: actual project preflight still guards arming.

POST `/internal/runtime/v1/shutdown` requires:

```json
{"version":1,"preserve_resume_requested":true,"reason":"cutover","acknowledge_coverage_gap":true}
```

Reasons are `shutdown`, `cutover`, or `rollback`. Acknowledgment defaults to
false; types are strict, boolean version/ack surrogates and unknown fields fail.
A cutover while armed requires explicit acknowledgment (409 otherwise). The
supervisor additionally refuses **automatic** switching while armed, regardless
of acknowledgment; that policy stays in its typed CutoverOptions. Normal
shutdown and rollback preserve intent without a preliminary disarm. The bridge
never calls `monitoring.disarm`; existing `MonitoringService.close` owns shutdown
and its opt-in resume preservation.

## Mutation admission and the arm race

After reading/validating a bounded public mutation body, wrap only its actual
mutation dispatch and response in:

```python
with bridge.mutation():
    dispatch_existing_post(payload)
```

Use the same gate for any PUT/PATCH/DELETE handlers or alternate command paths.
Do not hold it while a client uploads. The gate uses a condition/RLock and active
counter; it releases the lock while admitted work runs. Shutdown atomically
closes new admission, returns 202, then after the flush waits for admitted work
to finish. Later attempts raise 503 `runtime_quiescing`. Read-only status remains
available; `ready` becomes false while quiescing.

An admitted command may arm the guard after shutdown's initial check. After
all admitted work drains, unacknowledged cutover checks actual guard state again.
If newly armed, it aborts shutdown, reopens admission, and keeps the old writer
alive. The supervisor's actual-exit deadline consequently fails rather than
starting another owner. A 202 means accepted for controlled closure, not proof
that closure completed. An acknowledged explicit cutover proceeds and preserves
intent. If shutdown initiation fails, admission and writer ownership stay held;
there is no forceful fallback to a foreign PID or assumed-graceful signal.

The caller must keep any non-HTTP mutation entrypoints under equivalent admission
and preserve existing monitoring lifecycle locks. The bridge supplies no global
hardware readiness shortcut and no archive/schema migration.

## Focused checks

```sh
python3 -m unittest tests.test_runtime_bridge
python3 -m unittest tests.test_monitoring_operations tests.test_monitoring_guard tests.test_monitoring_preparation tests.test_monitoring_projects
```

The bridge tests use synthetic keys and temporary data only: cross-process writer
exclusion, environment/data path validation, duplicate/stale authentication,
strict HTTP framing, startup readiness/error completion, late mutation rejection,
committed-data drain, real armed shutdown/resume intent, late-arm cutover abort,
and failed cleanup retention through garbage collection. Central full-suite and
supervised Rust/Python cutover integration remain the parent integration gate.
