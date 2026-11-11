# Rust instrument adapters

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
`poll_spectrum`, `acquire_raw`, `capture_screen`, `tune`, `poll_receiver`, and
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

Do not report queued requests as successfully applied commands. Dropping the
pending reply receiver does not stop a command already sent. `cancel()` cancels
only that device and permanently invalidates its connection. Use `Close` for
orderly idle lab shutdown; cancellation cannot confirm RF off. Dropping all actor
handles drains already queued work, then closes the device.

`TinySaRequest`: `Prepare`, `Poll`, `AcquireRaw`, `CaptureScreen`, `Control`,
`ReadSd`, `ListSd`, `ReadTrace`, `Marker`, `ReadRbw`, `State`, `Close`.
`TinySaReply`: `Ready`, `Spectrum`, `RawSpectrum`, `Screen`, `Bytes`, `Text`,
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
