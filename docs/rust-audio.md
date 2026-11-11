# Core audio capture

This delivery slice provides the library and its recorded synthetic fixtures. Core/HTTP integration is delivered separately in stage 13; the integration interfaces below do not advertise an enabled Rust service.

`ori-audio` owns one bounded capture actor. It does not depend on Guard, Archive,
HTTP or Python. Core maps the existing `InstrumentControl::audio_inputs` and
`record_audio` callbacks onto this library and keeps the original permit and
acknowledged tune receipt in its `CaptureAuthorization` adapter. Constructors
neither enumerate nor open inputs. `NativeOptions::default()` disables native
capture even when the `native` feature is compiled.

## Integration

Create `AudioService::native(NativeOptions { enabled: explicitly_enabled,
limits: CaptureLimits::default() })`. Call `inputs().await` only on an explicit
input-list request. Each descriptor includes the exact name, ephemeral index,
original channel count, sample encoding and host-reported supported rate range. Capture
requires an explicit index plus byte-exact name; selection and format are checked
again at the actual open. There is no default microphone, channel mixing,
resampling or float-to-integer conversion. A float-only host input is unavailable
for this original PCM16 path rather than advertised as compatible.

`CaptureRequest` contains `input`, `sample_rate_hz` (8000, 12000, 24000 or 48000), exact `frames`
and `not_before: UtcTimestamp`. Core converts already validated seconds to frames
with Python-compatible ties-even rounding. The gate captures the original permit,
worker instance and applied tune revision and checks them synchronously from cache.
It runs before open, each accepted block and the final segment after stream drop.
Never replace the captured permit with a newer one during delayed execution.

`record(request, gate).await` returns `RecordedAudio` with complete PCM16 WAV bytes,
UTC start/end, rate, one channel and frame count. `start_capture` exposes a
`CaptureHandle` when explicit cancellation or a capture ID is useful. Dropping a
record future cancels ingress but leaves the owner responsible for actual native
release. No partial or discontinuous WAV is returned as evidence. Guard still
performs its own original-receipt check before the Archive commit.

Every capture starts with an empty, initially unarmed queue. Initialization
callbacks are discarded; ingress is armed only after open returns and its UTC
bound is sampled again. Frames before the later of actual open
UTC and the original acknowledged tune time are discarded; a straddling block is
trimmed upward to the next frame. Revocation or retune discards the entire pending
segment, and an old sink cannot feed a later capture. Start/end use six-digit UTC
strings, integer frame arithmetic and ties-even microsecond rounding. Native UTC
alignment uses CPAL's capture-to-callback delay and the system UTC clock; this is
host timing provenance, not calibrated ADC or RF timing acceptance.

## Bounds and cleanup

Defaults permit at most 30 seconds and 2880000 PCM bytes, with 8 input blocks of at
most 4096 frames and 16 live events. Resource limits are validated against finite hard caps; ring and live capacity
are at most 256 blocks/events, and a callback block is at most 65536 frames. A callback uses nonblocking queue admission and
never calls Archive, HTTP or a device actor. Its cached authorization callback
must obey the same restriction. Input ring overflow aborts the capture and emits
an explicit source gap. Slow live subscribers receive `LiveItem::Gap` and retain
service-global event sequence numbers; their lag never modifies a valid WAV.
Source status/errors, malformed PCM, unsupported formats, device loss, startup
panic and incomplete capture timeout are explicit errors.

`begin_close()` synchronously revokes ingress and returns a cloneable `AudioClose`.
Store that same handle and await `completed()` before Archive closure or writer
lease release. Its owned joiner survives waiter cancellation and resolves only
after the native stream has been dropped and its OS owner thread joined.
Native stop releases the stream directly: it does not require optional ALSA
hardware-pause support. Stop or
thread failures remain latched. A blocked backend drain remains visibly pending;
Core must retain ownership on its cleanup deadline rather than detach the stream.
Audio blocking work runs off the Tokio receiver polling executor.

## Native dependency and verification

The optional `native` feature pins CPAL exactly 0.18.2, the latest non-yanked stable
release reported by the official crates.io API on 2026-11-11 (published 2026-08-16;
MSRV 1.85). Sources: <https://crates.io/api/v1/crates/cpal> and
<https://github.com/RustAudio/cpal>. CPAL uses ALSA on Linux/Pi, WASAPI on Windows,
and CoreAudio on macOS with its default features disabled. Linux all-feature CI
needs `pkg-config` and `libasound2-dev` (`apt-get install -y pkg-config
libasound2-dev`). The matching extracted SDK used locally is selected through
`PKG_CONFIG_SYSROOT_DIR` and `PKG_CONFIG_PATH`; no system install is required.

Run `cargo test -p ori-audio --all-features` and
`cargo clippy -p ori-audio --all-targets --all-features -- -D warnings` after
preparing native build dependencies. All tests use raw PCM synthetic backends
through the actual owner/ring/segmentation pipeline, including exact Python
stdlib WAV/SHA fixtures, prehistory, revocation, gaps, blocked close cancellation
and receiver executor independence. Tests never enumerate or open hardware.
Native device behavior, analogue wiring, Pi support for a particular interface,
input permissions and actual audio quality require explicit physical acceptance;
compilation and synthetic tests do not establish them.
