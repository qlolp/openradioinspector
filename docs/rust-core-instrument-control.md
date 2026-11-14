# Core instrument-control binding

`crates/ori-core/src/instrument_control` binds the existing 29-operation
`ori-control` registry to the same Core-owned per-instrument `Worker` and serial
actor used for acquisition and receiver tuning. It opens no serial port and
constructs no audio backend or scientific process owner.

The runtime glue owns the module declaration, `Core.instrument_control`
`OnceLock`, startup, authenticated HTTP dispatch and lifecycle hooks. The binding
exports `initialize(&Arc<Core>)`, pure `handles`, `dispatch` with the original
`AuthReceipt`, `begin_shutdown()->CloseHandle`, `CloseHandle::completed`, and
`close_target` before DeviceManager teardown. The close attempt is retained and
failure remains sticky.

Admission retains the original LAN credential, Guard grant, worker instance and
connection. Omitted control revision binds once when that worker serially starts
the operation; an explicit stale revision fails before I/O. The original
cache-only check runs at every adapter I/O poll/wake boundary. Control callbacks
must resolve the exact opaque admission, never the most recent client for a
target. Credentials and manual tokens are neither events nor logs.

The public v1 wire retains the original typed-operation error text and integer
zero for an inactive lab lease; a live lease still reports its remaining seconds
as a float. Missing original lab permission has its specific Russian denial
before executor I/O. The original owner and cancellation are checked first, so
credential/Guard revocation is never relabeled as a missing lab lease. The same
original receipt and lease checks still run at every actual I/O boundary.

Normal commands share the existing bounded queue; `output.off` has one reserved
slot in the same executor. Publication ownership is reserved before I/O and is
bounded separately from retained artifacts. Actual control revision and public
committed revision are distinct. Terminal event and optional artifact commit in
one canonical Archive transaction; only its positive ACK publishes settings.
Completion order cannot regress either device status or lease-heartbeat status.

Guard manual handoff requires the existing explicit token, actual previous
worker JOIN and a once-only new worker bind after safe receive preparation. The
new analyzer worker remains acquisition-paused. Resuming a receive preset does
not release the manual grant or resume Guard; explicit manual return does that.
Configured metadata is available before connection, and receiver artifact reads
retain their existing scope/error precedence.

The complete connect/prepare/ready/bind sequence has per-target exclusion and an
original generation. A stop cannot open a replacement between ready and the
once-only bind. The exclusion ends before awaiting Guard coverage, so Guard's
project lifecycle cannot form the reverse lock order. Setup tasks remain owned
when the HTTP waiter disconnects. Shutdown joins them alongside coordinator
cleanup; a timed-out JOIN remains retained and makes later close attempts fail.

Other domain effects can capture `DeviceManager::capture_worker(target, gate)`.
Its opaque `RetainedWorkerHandle` exposes the original target, instance,
connection, cache-only `check`, and the same `execution_gate`. It does not pin a
pre-tune revision: a successful tune returns its separate post-ACK receipt.
`stop_retained_worker(&handle)` is cleanup authority over that captured Arc,
including after authorization revocation. It awaits actual worker completion and
transport drop and never selects a replacement. Queued tunes revoked before
their first checked I/O are discarded without stopping trusted acquisition;
revocation during an actual exchange closes and joins the original connection.

Admission bounds stay separate: 64 normal jobs, four per target, 128 retained
jobs, 2,048 permanent idempotency entries, and 32 MiB each for retained results and
in-flight blobs. Safe-off and lease heartbeat retain their own reservations.
The HTTP binding adds no common active-job ceiling ahead of these reservations.
Its weak admission registry is capped at 256 entries (128 jobs, 32 contexts,
32 lease owners and serial admission); setup records only its connection scope.
There are at most 32 strong connection scopes, 64 owned setup tasks and 128 weak
target lifecycle cells. Closed target tombstones are not retained indefinitely.

Scientific operations use the fixed `control_analysis` action and the same
bounded ResearchPool/configured worker. Analog LISTEN capture uses the same
AudioService with the original acknowledged lab execution; only advertised exact
mono PCM16 formats are selected. No resampling, mixing, synthetic receiver tune
receipt, source Sweep insertion, or hardware reopening is performed.

An acknowledged processed trace, zero-span trace or unsupported unit from the
automatic spectrum poll is preserved as an explicit complete JSON artifact in
the same transaction as one `device.acquisition.paused` event. The same actor
remains available for explicit lab work, but acquisition becomes paused/degraded
and the trace is never inserted as a canonical raw Sweep. A new object or
restart alone does not certify the physical trace mode as raw.

Capabilities follow the existing supported-versus-enabled contract in
`docs/api-v1.md`: an initialized service advertises `hardware_control`,
`tinysa_ultra_instrument_control` and `tinysa_ultra_async_operations` as supported.
With `DeviceOptions.transport=None`, `explicit_serial_adapters` and
`candidate.native_connector_enabled` stay false, and hardware validation stays
false. Cold capabilities, archive artifacts and input validation remain
available; native connection and control effects are explicitly unavailable.
Positive synthetic tests inject only a trusted `TransportFactory` into the real
Core. No production fake backend or automatic serial opt-in is introduced.

Validation uses private duplex transports, real temporary canonical SQLite,
trusted synthetic audio and the actual Python Controller-derived oracle. All
physical tinySA, AR-DV1, RF and host-audio validation remains unrun. These tests
make no Pi throughput, power-loss or hardware-readback claim.

The focused release gates include 30 Core library cases and nine real HTTP
cases. They cover every operation in the existing 29-operation registry against
36 original Controller cases, both held-terminal-commit orders, a real held
SQLite ready write, disconnected setup and tune waiters, original LAN and Guard
revocation, once-only manual bind, omitted versus explicit revisions, exact
artifact scope/errors and 17 original Python artifact-query number cases. The
no-transport HTTP case checks supported API flags, false connector/validation
flags and existing explicit
`400 unsupported_adapter` / `409 instrument_conflict` errors with zero opens.

The unchanged bounded library gates also cover normal-count, retained-input,
blob and full idempotency-ledger pressure while safe-off and heartbeat remain
available. PNG tests compare all 65,536 RGB565 words with the original Python
pixel conversion; compressed PNG encoding is derived, with the SHA of the actual
stored bytes. SD and WAV payloads retain exact bytes. Scientific cases execute
the installed isolated Python worker through the same research owner and retain
its original five methods. Control science uses Core's shared `private_worker`
helper with the original opaque admission receipt and `MemoryOnly` I/O class.
It registers with the same `BackgroundQos`: acquisition pressure rejects new
children and cancels running children, retaining registration and heavy admission
until the actual process and IPC tasks have joined, including a dropped waiter.
Only the exact trusted RPC argv receives the supervisor's shared-I/O capability;
its zero-byte, zero-charge receipt is validated and removed before the derived
reply is visible. Pure computation receives no invented logical I/O charge.
The separate aggregate-completeness capability remains unchanged.

Focused regressions use real Core/QoS state, original paired-credential
revocation and an owned blocking child with an observed PID. They check denial
before spawn, cancellation and actual reap before pressure completion, original
authorization checks before the worker deadline, and zero logical I/O charges.
The blocking fixture performs no scientific calculation; the existing 36-case
HTTP oracle separately verifies all five methods against the installed worker.
