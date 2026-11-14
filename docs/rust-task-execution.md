# Injected task execution ownership

`ori-tasks` remains the existing TaskBook planner and software scheduler. The
optional Core `task_execution` adapter owns real injected work through its
original authorization, scheduler epoch, worker connection and Guard receipts.
The ordinary software scheduler retains its documented default callbacks and
can be enabled without this optional producer. `control_task_scheduler`
describes that software lifecycle; `control_task_execution` separately describes
injected measurement execution. Absent producer configuration does not disable
software scheduling or imply a hardware result. A real injected producer must
use the same DeviceManager, Control coordinator and Legacy workspace and confirm
real outputs. Test producers establish ownership behavior only.

## Authority and saved formats

The original Python `TaskScheduler` checks that selected input files exist and
calls optional `on_start` / `on_complete` callbacks. It does not parse a file or
select a session, scenario or output. Its default callbacks perform no work.
Disable and close do not invoke a native-effect stop callback. Core must revoke
and drain the injected executor explicitly.

`tasks.py::FixedFrequencyList` saves
`format="openradioinspector-fixed-frequency-list", version=1`, with binary64
frequency/bandwidth values, action, profile, comment and assignment provenance.
Actions are `detect_only`, `measure_signal` and `record_phonogram`. Profile
validation returns warnings; it is not hardware authorization. The scheduler
does not consume this file format itself. Fractional legacy frequencies remain
unchanged; unsupported conversion to a native integer-Hz operation must fail
explicitly, without rounding.

Original PAN files are SQLite databases. `PanoramaStore` retains raw units,
binary64 levels in a little-endian BLOB, REAL frequency grids, metadata and UTC
recording times. Loading a PAN source through the Legacy facade does not prove
a new acquisition. TaskBook retains its existing 43 fields and version-1
envelope. Runtime bindings do not rewrite input files or add fields to TaskBook.
`protocol_file`, `equipment` and task names are not executable commands or
implicitly trusted output destinations.

## Injected interface

Core supplies an optional cloneable `TaskExecutionConfig`. It contains bounded
exact-name `TaskBinding` entries, mandatory trusted callbacks, an active-run
limit of at most 16 and a caller cleanup timeout of at most 30 seconds. A binding
selects a typed `TaskInput::FixedFrequencyList` or `TaskInput::Panorama` with an
operator-selected path and the existing session, stream and original raw unit.
Nonblank identities and units retain their original bytes, including surrounding
spaces and distinct NFC/NFD spellings. Byte quotas do not normalize those values.
These types contain private paths/callbacks and have no public JSON encoding.
HTTP cannot install callbacks or supply a command, scenario selector or output
path. The trusted callback selects its supported scenario and managed output.

`TaskExecution::admit_enable` receives the existing task DTOs and a
`TaskAuthority` wrapping the originating Core `AuthReceipt`. It refuses prior
RUNNING records and a live re-arm. Callback `capture` runs only at this admission
and retains the original worker connection and Guard receipt. The supplied
authority also includes this enable's immutable opt-in revocation flag; the
same check must be carried into the actual instrument actor. It never looks up
a replacement credential, worker or lease at execution time.

The first actual `TaskContext.epoch` binds the enable epoch. Every later callback
must match it. A changed epoch or input binding fails before preparing another
effect. `TaskTarget::prepare` is cache-only and returns an owned `TaskRun` before
any I/O. The executor registers that run before invoking `start`. Each injected
run implements actual `start`, `finish`, synchronous cancellation wake and an
awaitable `join` that confirms all producer, file, child and pipe work ended.
Cleanup targets the original retained worker instance, including after
revocation; a target string cannot select a replacement worker for stop.

`finish` returns bounded existing source IDs, session/stream, raw unit and UTC
interval. Empty/no-op results, changed scope/unit, invalid ordering or stale
authority cannot acknowledge completion. The source records and all scientific
math remain in the existing libraries; this executor creates no measurement IDs,
timestamps, spectra, PAN rows or derived results itself.

## Disable, cancellation and cleanup

`revoke` synchronously closes the original admission and wakes actual work.
`begin_drain` / `drain` let disable await all owned runs before acknowledging
`scheduler.configure(false)`. Successful drain permits a later explicit enable
only when no old RUNNING task can attach to fresh authority. Terminal
`begin_shutdown` / `close` also prevent new enable admission. Core closes this
executor before releasing Archive, Control, Legacy, worker and Guard resources.

Cloneable close handles wait on the same owned cleanup attempt. A caller timeout
reports pending cleanup and does not cancel or detach work. A failed join stays
sticky and retains the run and its leases; it cannot report cleanup success or
permit re-enable. Cancellation of a start/completion/close waiter does not
cancel the owned callback lifecycle. Original authority is checked again after
actual join before a normal completion is published.

`TaskCloseHandle::completed` bounds a caller's observation. An owned Core facade
uses `wait_joined` to await the same attempt's actual published result without
that observation deadline. A timeout cannot become the facade's final result;
an actual failed join remains a failure. Neither method starts another cleanup
attempt or captures new authority.

The new integration tests import only the new module by path, so they do not
need modifications to Core's thin task/HTTP glue. They use owned injected test
producers to cover admission, exact identities, epochs, revocation, blocked
cleanup, dropped waiters, invalid source receipts and sticky join failure.
They do not establish hardware, standalone CLI or filesystem power-loss
acceptance.

The source gate passed ten Rust behavioral tests with no ignored tests, plus
strict Clippy for the new module and its complete test target and targeted
rustfmt checks. The existing 22 Python TaskScheduler/FixedFrequencyList/
ControlTask authority tests also passed on Python 3.15.1. Rust verification used
a small standalone harness importing these exact new source files into the
existing Core target cache; it did not compile or claim acceptance of Core's
production callback/HTTP glue.
The identity regression creates and closes an actual Archive, preserves its
spaced session/stream/unit and NFC/NFD source data with exact binary64 values,
and then completes the same bound task without normalization.
