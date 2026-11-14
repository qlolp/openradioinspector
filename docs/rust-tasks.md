# Rust TaskBook scheduler

`ori-tasks` implements the existing local `ControlTask` software lifecycle.
The authority is `radioinspector/tasks.py`, `task_scheduler.py`, and the existing
`/api/v1/task-scheduler` routes. Eighteen crate tests pass against real files,
processes and Python oracles. Core HTTP integration and hardware acceptance
remain separate gates.

The scheduler never interprets the contents of task/panorama files, dispatches
commands, opens instruments, fetches URLs, or creates measurements. It checks
that the operator's existing local input path is a file. `NoopTaskEffects`
matches the existing server's default callbacks and confirms only software
lifecycle. Any real instrument adapter must use Core's original Guard lease and
worker-generation checks; this crate has no Archive, Session or Guard dependency.

Runtime SDK selection follows this distinction: `enabled:true` requires
`control_task_scheduler`, in addition to the TaskBook planning feature. It does
not require `control_task_execution`. That separate flag describes optional
injected measurement effects; software task completion never proves acquisition.

## Integration interface

`TaskScheduler::open(TaskSchedulerConfig, Arc<dyn TaskEffects>)` returns
`TaskResult<Arc<TaskScheduler>>` and must be called inside a Tokio runtime.
`with_clock(config, effects, Arc<dyn Fn() -> DateTime<Utc> + Send + Sync>)`
provides the shared deterministic clock. Neither constructor enables scheduling.

Public async methods return `TaskResult<Value>`: `status()`, `list_tasks()`,
`create_task(Value)`, `cancel_task(&str)`, `configure(Value)`, and
`run_once(Option<DateTime<Utc>>)`. `close()` returns `TaskResult<()>`.
`list_tasks` returns an array; Core retains its `{items, count}` HTTP wrapper,
envelopes, paths, percent-decoding, capability flags, HTTP201 creation, and
configuration body limits. Configure accepts exactly `{enabled: boolean}`.
Disabled/closed `run_once` returns JSON null. Public errors expose
`http_status: u16`, `api_code: String`, and `message: String`.

`TaskEffects::{on_start,on_complete}(&self, TaskContext) -> TaskFuture<'_, ()>`
receives an owned `ControlTask` and the original enable `epoch: u64`.
`TaskFuture` is a Send boxed future of `TaskResult<T>`. Callback context is not
Debug; it includes private local paths for an explicit operator-selected effect.
Callbacks must not recursively await mutations on the same scheduler actor.
Errors are stored as the task outcome, as in Python; adapters must return public
messages rather than secrets or private configuration. Rust callback panics are
isolated as a generic effect failure, without exposing panic payloads.

`ControlTask::from_dict(Value)` / `as_dict()` retain the existing full planning
record and profile fields. `ReferenceBook` implements independent per-kind
sequential IDs, ignores incoming reference IDs during reconstruction as Python
does, and retains custom kinds. User names, details, codes and IDs receive no
Unicode normalization. API task creation trims its name, following the authority.

## Ownership and bounded execution

One owned OS-thread actor serializes mutations and callbacks. Its bounded
mailbox also bounds the total admitted JSON bytes. File IO and fsync occur on
that thread, not on a Tokio acquisition executor. Tokio drives the callback's
future through its existing runtime. No task is spawned per scheduled task.
Canceling an HTTP/future waiter drops only its reply receiver; admitted work,
the callback, persistence, and the book lock remain owned.

Default limits are 16MiB per complete TaskBook/canonical state, 10000 tasks,
32 queued commands, 16MiB total admitted queued JSON, a one-second scheduling
interval, and a 30-second caller timeout. Config can lower book/task limits;
queue capacity is 1–64, interval is 0.1–60 seconds, and caller timeout is at most
300 seconds. An oversized or corrupt book is refused explicitly. Existing tasks
are never dropped to fit a limit. There is no automatic eviction or deletion.
The actor's `scheduler_alive` field retains the public active-scheduling meaning
of Python's optional background thread; the resident serialization actor can
wait while scheduling is disabled.

Due tasks run in insertion order. End time is scheduled `start_at + duration_s`,
not acknowledgment time. A newly started task completes on a later pass, even
when its scheduled end is already past. Missing files and effect failures abort
that task while allowing independent tasks to proceed. Disable preserves running
task state, stops further task selection, and acknowledges only after the
original in-flight effect has finished. Re-enable can complete the existing task
without invoking its start callback again. Enable epochs reject queued passes
from an earlier pause/resume. Repeated enable does not restart a healthy timer.

On startup, previous RUNNING records become aborted with
`аварийное завершение приложения`; enable is runtime-only false again.
Running state is durably saved before the start callback. This closes Python's
window where a crash during the callback could leave a queued file record;
normal completion/error DTOs and counters retain authority behavior.

## File durability and shutdown

The exact supplied TaskBook filename is used. Its companion `<filename>.lock`
uses OS-exclusive locks: flock on POSIX and overlapping byte-range locks on
Windows, compatible with Python's implementation. Lock-file presence alone is
not ownership, and the lock file is not deleted at release.
The saved envelope remains `format="openradioinspector-task-book", version=1,
tasks=[...]`; ReferenceBook remains its independent model rather than an invented
TaskBook field. The existing 43 task fields and ten profile fields are retained.

Save uses an unpredictable sibling temporary file, preserves existing POSIX
mode or defaults to0600, writes the complete bounded JSON, flushes and fsyncs the
file, and replaces the destination atomically. POSIX also fsyncs the parent
directory and checks the authority's owner/non-writable-parent boundary.
Windows uses `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)` after the file flush;
POSIX directory fsync has no portable Windows equivalent. Hosted filesystem and
power-loss acceptance are separate gates. A failure after rename can leave the
new complete file with uncertain durability; a failed save never exposes a
partially written destination, and retry persists the same desired state.

Close stops admission/selection, awaits the original callback, aborts remaining
running tasks with `принудительно завершено оператором`, persists, releases the
OS lock, and awaits an owned thread join. Cancelled close waiters do not cancel
cleanup. A failed abort/save retains the original lock and disabled actor for
`close()` retry; no new owner can overwrite it. This deliberately strengthens
Python's finally-release behavior on close-save failure. A caller timeout reports
`task_shutdown_pending`; it does not report shutdown success or drop the actor.
Core must await successful close before releasing its runtime/archive resources.

## Reproduction

`contracts/fixtures/task-generate.py` executes the actual Python scheduler and
TaskBook in owned temporary directories. It records source/generator hashes and
69 deterministic actions, full planning fields and ReferenceBook behavior.
The current `task-scheduler-v1.json` normalizes only temporary root paths to
`$TMP`; private paths never enter the public DTO. The preserved pre-Rust fixture
`task-scheduler-before-v1.json` has SHA256
`cd792b1ae4deb85f51e50c2195721c2e7705a29884f7becacf40f2b83f957a27`.
The first test exposed a generator defect: its tuple arguments bypassed temporary
path substitution. The before file remains unchanged, including those synthetic
owned temporary names. `task-fixture-portability-diff-v1.json` records that only
those temporary prefixes and generator metadata changed; Python source hashes
and all behavior values remain equal. The corrected fixture SHA256 is
`be55970ed205e2dfbdc6e81bb6141f3e0f7a3a43ed5789ba8aa37cf078c43ee6`.
Without `--output`, the generator writes stdout for reproduction comparison.

The separate `task-edge-generate.py` oracle contains 18 ISO forms, nine legacy
coercions and eight duration boundaries. Tests cover Python's accepted basic/week
dates, arbitrary single-character date separators, microsecond timezone offsets,
Unicode decimal digits in numeric strings, half-even duration rounding and exact
overflow outcomes. User text and reference IDs are never normalized by those
numeric conversion rules. Non-finite or unrepresentable legacy book data is
refused explicitly, retaining the original file rather than dropping records.

The 18 Rust tests cover all of the above, real background ticks, queue/byte
budgets, preserved permissions, failed-save rollback, cancellation-safe close,
original enable epochs, actual Python process locking, and killed Rust/Python
owner recovery. The Rust callback-boundary test self-invokes its real parent test
with an owned environment phase marker; no ignored or empty helper acceptance
test is present.

```sh
PYTHONPATH=. python contracts/fixtures/task-generate.py > /tmp/task-oracle.json
PYTHONPATH=. python contracts/fixtures/task-edge-generate.py > /tmp/task-edges.json
CARGO_BUILD_JOBS=2 cargo +1.99.1 test -p ori-tasks -- --test-threads=2
CARGO_BUILD_JOBS=2 cargo +1.99.1 clippy -p ori-tasks --all-targets -- -D warnings
```

The process tests use `python3`, overridable by `ORI_TASKS_TEST_PYTHON`, and the
actual repository Python modules. They do not silently skip a missing runtime.
All runtime files and processes belong to temporary test directories. Process
callback/readiness deadlines remain two/five seconds; no performance or Pi
acceptance claim is inferred from running these tests on a shared Linux x64 host.

Dependencies use existing workspace versions only: chrono0.4.45, fs2 0.4.3,
serde1.0.229, serde_json1.0.154 with ordered maps/arbitrary precision, Tokio1.54.1,
tempfile3.27.0, Unicode15 category table0.6.0, Unix libc0.2.191 and Windows
winapi0.3.9. Integration owns the
workspace Cargo.lock update; this isolated slice does not edit it for publication.
