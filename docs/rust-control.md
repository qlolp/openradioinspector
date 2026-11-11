# Rust instrument-control coordinator

This delivery slice provides the library and its recorded synthetic fixtures. Core/HTTP integration is delivered separately in stage 13; the integration interfaces below do not advertise an enabled Rust service.

The intermediate lock retains the inherited compatible `futures-util 0.3.34`.
Authorized offline metadata activates its required cached `futures-macro
0.3.34`; it changes no inherited package version or checksum. Core's exact
`futures-util 0.3.32` constraint is reviewed separately when stage 13 lands.

Status: implemented library, verified on owned synthetic transports and temporary
SQLite archives. Hardware validation is **not run**.
Base: `21f9c20036fdd0c40f84f2d5adc7eb4278e69ffd`.

## Source contract and ownership

The Python oracle is `radioinspector/tinysa/api.py`, `controller.py`,
`commands.py`, `forms.py`, `protocol.py` and the owned `UltraWire` fixtures.
There are 29 operation names and 29 settings. HTTP submission returns 202;
polling exposes queued/running/succeeded/failed/cancelled/unknown. Submission
and listing omit `result`; an operation GET includes it, including null for a
pending job. A lab acquisition result reveals its lease only to the matching
private `X-Instrument-Client` capability. Nothing exposes that capability or
lease in status, events, logs, public lists or stored artifacts.

`ori-control` owns bounded operation admission, ephemeral idempotency,
exclusive lab-lease metadata, wire views and artifact preparation. It will not
open a port, construct an adapter/actor, implement another acquisition loop,
write SQL or implement independent detection/science algorithms. Core's same
owned per-instrument executor runs all device requests and ordinary polling.
Core's existing outer task JOIN and owned transport-drop proof remain required.

Only the released Rust request/control/setting enums are executable. The AR-DV1
boundary is Prepare/Poll/Tune/State/Close, FM/AM and the explicitly selected
VR2504A monitoring modes. The documentation command matrix does not activate
memory, system, TETRA, key, IQ or other unsupported commands.

## Frozen authorization boundary

Admission retains an immutable, opaque **owner receipt** containing the original
Core execution gate, worker/connection identity and authorization/lease epoch.
It is neither Debug nor Serialize. Queuing or awaiting must never replace it
with a current worker, token, epoch or newly issued permission.

An omitted `expected_revision` binds the current control revision **once at
serialized execution start**, after validating that original owner receipt and
before the first I/O. An explicit stale revision fails without I/O. The bound
execution revision then remains fixed across all awaits. Pure lease renewal
does not wait behind I/O or change the control revision; it still validates the
same active lease identity and expiry. This preserves legitimate queued Python
commands without refreshing their authorization.

The accepted additive adapter API is:

```rust
pub type ExecutionCheck = Arc<
    dyn Fn() -> ori_instruments::Result<()> + Send + Sync + 'static
>;
TinySaActor::enqueue_checked(TinySaRequest, ExecutionCheck);
ArDv1Actor::enqueue_checked(ArDv1Request, ExecutionCheck);
```

The exact same callback runs on every read/write/flush poll and after each I/O
wake/completion. A queued denial sends no bytes and preserves known state.
Denial during an incomplete exchange taints the connection and exposes unknown
state under the existing rules. Policy error text is fixed and contains no
caller text or token. The scoped callback is removed before trusted Close
cleanup. Existing enqueue/enqueue_guarded callers keep their behavior.

## Core callbacks

The coordinator receives `Arc<dyn ControlExecutor>` and
`Arc<dyn ControlCommitter>`. The executor is implemented in Core, with no
dependency from this crate back to `ori-core`.

```rust
trait ControlExecutor: Send + Sync {
    fn snapshot(&self, target: &Target) -> Result<CachedInstrument>;
    fn execute<'a>(&'a self, request: ExecutionRequest) -> ControlFuture<'a, ExecutionOutcome>;
    fn cancel_and_join<'a>(&'a self, request: CancellationRequest) -> ControlFuture<'a, CancellationOutcome>;
    fn coverage<'a>(&'a self, request: CoverageRequest) -> ControlFuture<'a, CoverageOutcome>;
    fn analyze<'a>(&'a self, request: MeasurementRequest) -> ControlFuture<'a, MeasurementOutcome>;
    fn audio_inputs(&self) -> ControlFuture<'_, AudioInputs>;
    fn capture_audio<'a>(&'a self, request: AudioRequest) -> ControlFuture<'a, AudioOutcome>;
}
trait ControlCommitter: Send + Sync {
    fn commit<'a>(&'a self, batch: ControlCommit) -> ControlFuture<'a, CommitOutcome>;
}
```

`ExecutionRequest` carries the original owner receipt, optional expected
revision, a single typed operation/validated plan, and a combined scoped check
for that same owner plus lab lease. Core binds the execution revision in its
owned command loop. The request never carries a raw console command.
`ExecutionOutcome` retains the existing typed adapter reply, actual cached
state/revision, operation interval and taint/uncertainty classification.
Cancellation targets only the originally admitted worker and must prove its
pending I/O joined; it must not stop a replacement worker. Coverage callbacks
coordinate Core/guard ownership; the coordinator does not create another guard.
Scientific and audio effects reuse the existing Core-owned services. Unavailable
callbacks return an explicit capability/profile error before any unsupported I/O.

Production HTTP uses `submit_admitted(CachedInstrument, request, client_capability)`:
Core passes the same atomically captured snapshot with a backend-created opaque
owner receipt additionally wrapping the **original LAN credential revocation
receipt**. Identity getters delegate the captured owner; revalidation checks both
authorities synchronously without thread-local state, a fresh snapshot or public
string reconstruction. The default local/oracle submit path captures a snapshot
once before its first await. Guard-claimed targets conservatively report the
missing explicit manual/coverage bridge as unavailable until Core implements it;
they never steal a manual token, silently reopen a worker or claim full parity.

`ControlCommit` is one atomic operation event plus optional closed artifact
bytes/hash/metadata. The Core implementation uses its canonical writer.
Queued/running/terminal views and artifact references become visible only after
the corresponding commit. A terminal persistence failure keeps the last
committed view, latches an error, prevents replaying the hardware mutation and
makes close report failure. It never fabricates a success or publishes a
reference to uncommitted bytes.

## Typed workflows and bounds

Operations are a closed enum matching the Python allowlist. Request validation
keeps the existing admission-vs-asynchronous-failure boundary; complete typed
parameters and preset contents are validated before the first device mutation.
Settings reuse `ori_instruments::Setting`. Preset import needs an additive
typed receive-preset operation: validate every non-RF setting and merged range
first, then issue the documented combined `sweep start stop points`, preserving
the Python transcript and its explicit lack of rollback. No raw-console escape
or new hardware command is introduced.

Screen capture retains the received RGB565BE bytes while preparing the existing
480x320 RGB PNG representation, exact integer pixel conversion, timing and hash.
Saved artifacts commit before publication; an unsaved preview remains bounded
ephemeral data. Raw scans retain integer Hz, f64 values, units, zero readback,
source wire and host interval. Processed traces/zero-span remain artifacts, not
canonical monitoring sweeps. Measurement methods delegate to the existing
scientific worker; audio uses the existing selected-input service.

Defaults: four normal jobs plus one reserved output-off slot per
instrument, independent instrument queues, at most 32 instrument contexts and
64 normal active jobs globally (plus at most 32 reserved output-off jobs),
128 retained jobs, 2048 mutation idempotency records, 64 KiB request bodies,
8 MiB individual blobs, 32 MiB in-flight blob reservations and 32 MiB retained
result/input bytes. Cached status and lease renewal do not wait for blob budgets.
Preview reuse is at most 500 ms and never consumes the mutation ledger. Lease
renewal, output-off, inspect, guides and audio-input reads do not fill it either.
History eviction keeps the mutation ledger, so a reused request ID whose history
expired fails rather than replays I/O. Closing first rejects admission, cancels
only owned operations, joins every background task and propagates any latched
failure on repeated close. No automatic RF, worker reopen or monitor resume.

## Integration and verified behavior

The crate is a root-workspace member through the existing `crates/*` pattern;
no root manifest, Core, HTTP, Guard, Session, archive or client source was changed.
The only lock additions are this crate, PNG 0.18.1 and its compression dependencies,
plus the existing tokio-util runtime feature's futures-util edge. Existing exact
versions are retained. Production code has no Python, serial-open or SQL path.

Core maps the existing routes without changing envelopes: capabilities to
`capabilities`, settings/control-lease to cached `status`, operations to
`submit_admitted`/`list`/`get`/`cancel`, and screen to an unsaved capture submission
with HTTP 202. POST settings and lease aliases retain their current Python
normalization. Artifact list/get/content remain Core's existing canonical archive
queries; the coordinator prepares writes only. Cold configured/unconnected
metadata remains honest, and commands require an actual owned receipt.

At the same owned serial execution start, Core calls `BoundExecution::bind` with
its current **control** revision, then `plan_operation` with that same owned
adapter's cached state. Control revision is distinct from transport/tune revision.
Apply `RevisionEffect` exactly once after all ACKs, checking overflow before I/O.
Scope every adapter command with the same original request check, augmented by
the single bound execution revision; never select another worker or permit after
an await. `classify_adapter_error(error, actual_restart_required)` preserves
before-wire denials versus uncertain exchanges and explicit unavailable profiles.
Public Display/Debug omit dynamic backend/transport error text.

`AudioRequest` carries the original `BoundExecution`, actual acknowledged
`TinySaReply::State`, its interval and the same lease/credential check. This is
tinySA analog LISTEN at explicitly selected 8/12/24/48 kHz, not a fabricated AR-DV1
tune or USB PCM. The Core callback owns its selected native audio resource,
checks every block and final frame, and joins it on cancellation. The library
validates closed mono PCM16 RIFF format, rate and the complete requested frame
count without changing WAV bytes. All five scientific methods delegate their
exact raw sample and actual RBW to Core's scientific worker; no math is forked.

The 32 MiB retained byte limit includes reserved heartbeat space and one bounded
output-off lane per context. Normal inputs, previews and results cannot consume
those reservations. Blob reservations survive conversion **and** the terminal
writer ACK. These are logical owned-byte/count limits, not a measured process-RSS
or Raspberry Pi performance guarantee. Cached reads do not acquire blob permits.

A lease keeps the original granting credential receipt as well as its epoch.
A later valid credential cannot refresh that original authority. Read-only
screen/SD calls grant no lab mutation, but their already-active lab epoch still
constrains in-flight liveness. Renewal extends only the same live original lease.
Idle expiry requests safe-off through the same executor and keeps lab paused.
Expiry/revocation during a silent read actively cancels and joins the original
worker instead of waiting for the 90-second binary deadline. Expiration events
contain no secrets. Cleanup never resumes acquisition or opens a replacement.

Queued cancellation waits for its committed cancelled view and sends no I/O.
Close cancels, joins all original admitted contexts including idle lab, verifies
actual transport drop and joins its own tasks/timer. A repeated successful close
does not stop an already completed owner again; a failure remains sticky even if
a later cleanup succeeds. In-progress admission commits are bounded by the same
close deadline. Core must own an admitted submission future to completion and
retain its port/writer leases whenever close reports failure.

Terminal persistence failure preserves the last committed view, disables new
control admission and performs trusted cleanup of the original worker; it never
replays hardware. Trusted Close must remain usable after revocation, clear the
scoped callback and attempt orderly RF/CAL off only on an untainted idle lab
connection. Core's real outer JOIN plus transport-drop proof is required;
an adapter Close ACK alone is insufficient. Screens use canonical PNG-only
artifacts with source RGB565BE SHA/format/dimensions and actual PNG SHA. The
exhaustive 65,536-word pixel oracle proves exact Python conversion and reversible
source pixels; original compressed transport packets are not claimed retained.

Preview reuse is bounded to 500 ms, owned connection, control revision and
admitted display-changing generation. It never returns an old frame after an
accepted control mutation. Previous completion-proof keys are retained only
while referenced by bounded contexts/jobs, so reconnect history cannot grow
without bound.

The explicit Guard manual/coverage bridge and production Core HTTP/executor
binding remain Core integration work. `guard_claimed=true` with no bridge is
visibly unavailable; no secret manual token is inferred or worker reopened.
This temporary conservative denial is not claimed as full manual-Guard parity.
Capabilities expose unavailable science/audio callbacks before unsupported I/O.
AR-DV1 tinySA operations are unavailable; existing selected receiver registry and
Core Tune/Poll remain authoritative. No memory/system/TETRA/IQ commands are added.

## Reproduce verification

`tests/generate_oracle.py` invokes the actual Python Controller on UltraWire and
its existing scientific functions, using a selected in-memory analog WAV source.
Its 36 cases cover all 29 operation names and all five scientific methods. The
Rust test executes the real TinySaActor on an owned duplex, compares command
bytes, result shapes and source/artifact documents, atomically writes SQLite,
reopens exact PNG/SD/WAV bytes and proves no processed/raw control artifact was
inserted as a canonical Sweep. `tests/generate_queue_oracle.py` uses actual legacy
HTTP + DeviceWorkerManager serialization for omitted/stale revisions and queued
revocation. It creates its own loopback server/archive and closes every resource.

Focused checks also cover full ordered preset validation with zero-byte failures,
reserved-key JSON object/scalar confusion, f64 signed zero/subnormal serialization,
lease privacy, expired idempotency history, normal count/byte pressure, heartbeat
and reserved off, held terminal writer ACK, active expiry of a silent binary
read, real cancellation/drop barriers, and artifact FK rollback with no terminal
event/reference. Hardware, RF, power loss and target performance remain unrun.

```sh
PYTHONPATH=. OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python crates/ori-control/tests/generate_oracle.py
PYTHONPATH=. OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python crates/ori-control/tests/generate_queue_oracle.py
cargo test --locked -p ori-control -- --test-threads=2
cargo clippy --locked -p ori-control --all-targets -- -D warnings
cargo test --locked -p ori-instruments --features serial -- --test-threads=2
cargo clippy --locked -p ori-instruments --all-targets --features serial -- -D warnings
cargo fmt -p ori-control -p ori-instruments -- --check
```
