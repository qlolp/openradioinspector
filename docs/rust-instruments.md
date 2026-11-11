# Rust instrument adapters

This delivery slice provides the library and its recorded synthetic fixtures. Core/HTTP integration is delivered separately in stage 13; the integration interfaces below do not advertise an enabled Rust service.

`ori-instruments` is a standalone Rust 1.99.1 / edition 2024 library. It owns
one asynchronous transport per adapter and never writes SQLite, assigns archive
IDs, or discovers ports. Its default build has no physical serial dependency.
Only the explicit `serial::open(&SerialOptions)` connector (feature `serial`)
opens a supplied native path; the core must hold exclusive hardware ownership
before calling it. No test enumerates or opens a physical port. The only native opening test
allocates its own PTY pair and verifies rejection of a Python-style advisory
lock without changing termios.

## Integration API

`TinySa<T>` and `ArDv1<T>` accept any
`AsyncRead + AsyncWrite + Unpin + Send` transport. Constructors validate a typed
`ReceiveConfig` / `ReceiverConfig` and `IoLimits` before I/O. `prepare`,
`inspect`, `poll_spectrum`, `acquire_raw`, `capture_screen`, `tune`, `poll_receiver`, and
`close` are async methods. `state()` and `cancellation_handle()` are immediate. Actor `state()` returns a
cached snapshot without queueing behind I/O; active mutations expose unknown
mode until complete acknowledgement.

`SpectrumSample` contains `Vec<u64>` integer-Hz frequencies, `Vec<f64>` levels,
original `level_unit`, typed `SpectrumKind`, `settings` JSON and `OperationInterval`.
`RawSpectrum`, `ProcessedTrace` and `ZeroSpan` are explicit; processed traces
and zero-span artifacts must not be written as canonical raw monitoring sweeps.
`RawSpectrumSample` additionally retains the exact scanraw wire bytes and zero
readback. `ReceiverSample` retains integer-Hz frequency, requested mode, the raw
`u32` S-meter count, `s_meter_count` unit, raw squelch status, decoded-mode evidence,
raw MD status and metadata. The core supplies canonical IDs and archives results.
Intervals are host operation intervals; serialization emits RFC3339 UTC with
exactly six fractional digits. They do not imply per-sample timestamps.

The core can own adapters directly or use `TinySaActor::spawn(adapter, capacity)`
and `ArDv1Actor::spawn(adapter, capacity)` (capacity 1–1024). Each creates its own
Tokio task and bounded queue. `enqueue(request)` returns immediately with a
`Pending<Reply>` or `QueueFull`; `.result().await` waits for actual I/O completion.
Use `enqueue_guarded(request, closure)` for user operations. The core supplies a
quick synchronous `FnOnce() -> Result<(), InstrumentError> + Send + 'static`
closure which rechecks lease/token, global guard, revision and reconnection
generation immediately before dispatch, after any queue wait. A rejected guard
sends no device bytes, leaves connection state intact, and returns the bounded
`PolicyDenied` variant without the guard's text or tokens. The adapter keeps no
second lease authority. Unguarded `enqueue` is for an already authorized internal
path such as system close. The core must also cancel an active exchange when its
authorization expires during I/O; cancellation taints the connection.

`enqueue_checked(request, ExecutionCheck)` is an additive stronger path for
Core-owned user operations. `ExecutionCheck` is one immutable
`Arc<dyn Fn() -> Result<()> + Send + Sync + 'static>` callback. The same callback
runs after queue wait, before/after every read, partial write and flush poll,
and before an exchange returns success. It must only revalidate the original
cached owner/connection/lease epoch and the once-bound execution revision; it
must never issue a fresh permission or mutate a revision on each invocation.
Calls must be quick and nonblocking, and their error text is never exposed.
A denial before dispatch sends no bytes and preserves state; an interrupted
exchange taints the connection and leaves input/output knowledge unknown.
The callback is scoped to that single request and is removed before trusted
Close cleanup. Existing enqueue/enqueue_guarded behavior is unchanged.

A policy callback is not an expiry scheduler. Core still owns lease timers and
must cancel/wake a silent pending exchange when authorization expires. The next
I/O poll always revalidates the original callback, including after a wake;
cancellation does not confirm RF off or authorize a retry. Owned in-memory
transport tests cover pending reads, partial writes, revocation during the final
read, queued old epochs, isolation between instruments and actual transport drop
after trusted Close. No physical device is involved.

Do not report queued requests as successfully applied commands. Dropping the
pending reply receiver does not stop a command already sent. `cancel()` cancels
only that device and permanently invalidates its connection. Use `Close` for
orderly idle lab shutdown; cancellation cannot confirm RF off. Dropping all actor
handles drains already queued work, then closes the device.

`TinySaRequest`: `Prepare`, `Inspect`, `Poll`, `AcquireRaw`, `CaptureScreen`, `Control`,
`ReadSd`, `ListSd`, `ReadTrace`, `Marker`, `ReadRbw`, `State`, `Close`.
`TinySaReply`: `Ready`, `Inspection(TinySaInspection)`, `Spectrum`, `RawSpectrum`, `Screen`, `Bytes`, `Text`,
`Directory`, `RbwHz`, `State`, `Closed`. `ArDv1Request`: `Prepare`, `Poll`, `Tune { frequency_hz, mode }`,
`State`, `Close`; replies are `Ready`, `Observation`, `State`, `Closed`.

`TinySaControl` is typed, with explicit `EnterLab`, `OutputOff`,
`ResumeMonitoring`, `Set`, typed trace operations, manual screen, preset,
confirmed SD deletion, self-test and explicitly confirmed generator sweep.
All existing semantic settings are validated and converted to their shell units.
Wire-level trace/marker/RBW readbacks and SD files preserve device evidence;
portable presets, analysis, artifact encoding and audio belong to core/services.
The core must validate global guard state, exclusive leases, revision and explicit
RF authorization before dispatching a lab/RF command. The adapter independently
enforces monitoring versus lab control and acknowledged input/output/unknown
hardware mode. It does not implement a second lease authority.

## Inspection and fresh monitoring provenance

`inspect()` returns `TinySaInspection { state, commands }`; commands are sorted,
deduplicated tokens using Python's actual `[a-z][a-z0-9_]*` help parsing. There
is no invented mandatory command set. A valid empty help response yields no
commands; individual typed operations still require their command in help.
`control_profile_verified` means identity and the framed help query succeeded.
The existing `profile_verified` flag separately records successful explicit
receive preparation. A cold query can establish the first flag without the
second and without establishing input mode or RF-off evidence.

Cold inspection sends only `info`, `version`, `deviceid`, `help`; prepared
inspection sends only `help`, and cached inspection sends no bytes. It never
scans, reads RBW/zero, enters lab, or sends RF/mode/settings controls. This is an
intentional safety difference from Python's cold `Controller._profile()`, which
also calls `_prepare()`. The Rust core must call `Prepare` separately when its
guard authorizes preparation. Existing input/output/unknown knowledge and source
flags survive inspection unchanged; a cached query cannot repair unknown state
or a tainted connection. The normal actor execution guard still runs after the
queue wait, including for cached queries.

`legacy_monitoring_settings(&SpectrumSample, &TinySaState,
Option<&TinySaProvenanceContext>)` is an explicit **fresh-source-only** metadata
projection for the core's live DTO factory. Context contains the genuine core
`connection_id` and controller `revision`; it is required for control-profile
samples and forbidden for receive-only samples. The helper preserves the actual
Python receive/control settings shape while leaving the internal evidence
metadata and all arrays, units and operation intervals available unchanged.
The initial controller settings omit an implicit `trace_unit` key until that
setting is explicitly acknowledged, matching Python; dBm remains the documented
interpretation fallback for the unprocessed receive path.

Python validates `deviceid` but records only `info`/`firmware` in sweep identity.
Its receive-only `generator_output=off` plus `generator_output_source=
command_acknowledged` records setup acknowledgements with readback false. Control
context instead records `unknown` / `no_physical_readback` and the controller's
settings/mode/provenance object. Rust-only diagnostic root keys such as
`device_mode`, `instrument_settings` and `artifact_kind` are not silently added
to that legacy settings fingerprint. Those details remain in the original live
sample and typed state.

Projection refuses processed/zero-span/non-dBm output, taint/closed/unprepared or
unknown/output mode, unacknowledged or mismatched state, mismatched context and
unmapped metadata extensions. It is not a physical compatibility certificate.
Never call it on historical rows, change archived settings/baselines, or turn an
incompatible baseline into an accepted one by normalization. A baseline lacking
the current acknowledged provenance must fail the normal compatibility gate.

The owned `instruments-inspection-v1.json` fixture is produced by the actual
Python adapter/controller on `UltraWire`, not by a second command or fingerprint
implementation. Its seven sweep cases preserve exact metadata, float bits,
first tied/global peaks and command order. `generate_inspection_fixtures.py
--check-rust EVIDENCE_JSON` compares emitted Rust live samples with the actual
Python `Sweep` and detector `fingerprint` functions. Fixture IDs/comparison time
are separate from Rust's independently validated real UTC operation interval.

## Evidence and safety

An acknowledged command is never physical readback. The exact tinySA model and
firmware profile is required before setup; conservative monitoring is limited to
100 kHz–800 MHz, 2–290 points, and requested 10/30 kHz RBW. These RBW requests are
not measured applied values. No receive workflow enables RF generation.

`output off` preserves output or unknown hardware mode. Only successful prepare
or explicit `ResumeMonitoring` establishes acknowledged input mode. Raw and
zero-span acquisition require acknowledged input. Manual UI changes, hardware
preset recall and self-test invalidate mode knowledge. The adapter can invalidate
knowledge explicitly after an externally reported change. Communication failure,
timeout, cancellation or dropping an in-flight exchange permanently taints the
connection; later commands cannot flush/retry and consume a stale prompt or ACK.

Manual/processed trace provenance remains processed after monitor resume; an
input ACK does not reconstruct unknown trace settings.

Text and binary replies have bounded size and total deadlines. Quiet gaps do not
end tinySA replies. The pinned terminal is all four bytes `ch> `, including
the final space; a fragmented terminal is fully consumed. Binary payloads are read by exact size, exact command echo and
terminal prompt; ASCII-looking payload bytes are data. Scanraw preserves the
firmware's little-endian samples, read-back zero and float32/open-stop grid.

AR-DV1 legacy mode supports FM/AM. `ardv1-monitor-v1` requires VR2504A and allows
AUTO/DSTAR/YSF/ALINCO/NXDN/P25/DPMR/DMR/FM/AM through the existing MD commands.
AUTO excludes TETRA. MD is retained as receiver decoder evidence, not independent
identification of a network or emitter. LM counts are not converted to dBm.

## Verification status

The inspection/provenance follow-up passes 49 synthetic adapter tests plus the
one owned-PTY exclusion test on Rust 1.99.1 with all features, fmt and Clippy
warnings denied. Its Rust-emitted seven live sample results also pass the actual
Python `Sweep`/detector fingerprint/float-bit/first-peak comparison on Python
3.12 and the isolated modern Python 3.15.1 environment. The inspection fixture
is deterministic, SHA-256
`2e903bd53d433e4988fc549618a5143cb221f951894012ce41f56014e7646cbb`.
The fixture records source hashes and the deliberate cold-inspection difference.

To reproduce the cross-language check, give the adapter test an explicit scratch
file (no archive is opened), then run its independent Python verifier:

```sh
ORI_INSPECTION_EVIDENCE=/tmp/ori-inspection-samples.json cargo test -p ori-instruments --all-features
python crates/ori-instruments/tests/generate_inspection_fixtures.py --check-rust /tmp/ori-inspection-samples.json
```

Implementation is tested with synthetic `AsyncRead`/`AsyncWrite` transcripts
based on existing Python golden behavior, including partial writes, delayed and
malformed frames, raw binary decoding, RF-mode invariants, taint and independently
progressing actors. These fixtures were authored for tests; they are not captured
physical traffic. Physical validation is **not run**. This crate does not claim
hardware readiness, RF accuracy, measured throughput, IQ, USB audio, or expanded
receiver capabilities. Hardware acceptance remains the separate project gate.

Run from a workspace with `cargo test -p ori-instruments --all-features`, or use
`cargo test --manifest-path crates/ori-instruments/Cargo.toml --all-features`
while the crate is standalone. Dependencies are exact-pinned; the root workspace
lockfile becomes authoritative when integrated.

The optional connector pins tokio-serial 5.5.0 and serialport 4.10.1 with default
features disabled. The audited Unix open path takes `TIOCEXCL` and nonblocking
exclusive `flock` **before** terminal configuration; Windows opens an exclusive
handle (`CreateFileW` share mode zero) before DCB changes. The PTY regression
verifies advisory-lock denial and unchanged termios. This is cooperating
cross-process ownership, not a substitute for the core cutover lease; DTR line
behavior on physical opening remains OS/driver dependent and physically untested.

`ApplyReceivePreset(ReceivePreset)` validates the entire ordered receive-only
setting list and merged frequency/LNA context before any I/O. It uses the
existing Python order: RF/CAL off, input mode, LNA off, explicit Ultra selection,
one combined sweep range/point command, ordered remaining settings and optional
LNA on. Every command is checked against verified help before mutation. State is
published after acknowledgements; interrupted sequences taint under the existing
exchange rules and have no device rollback. Confirmed safe-off is retained even
if a previous lab output setting was on.

`ReenterLab` is an additive explicit pause/off operation for a new lease while
the device remains in lab after release or expiry. Existing `EnterLab` still
rejects an already active lab mode. The coordinator owns lease authorization;
the adapter does not create a token or refresh authority. Both extensions match
real Python Controller traces generated by `tests/generate_control_fixtures.py`
on the owned in-memory UltraWire. Three duplex tests cover exact command order,
merged state, invalid/duplicate/unsafe presets with zero I/O, and the unchanged
old EnterLab rejection. Hardware remains untested.
