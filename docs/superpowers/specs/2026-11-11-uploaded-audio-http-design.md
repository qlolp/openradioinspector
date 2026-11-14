# Uploaded PCM audio HTTP compatibility

The existing Python service records uploaded normalized samples and distributes
derived PCM. These seven routes do not start native capture. Add them to the Rust
candidate through a new private `audio_http` module, leaving existing Core,
router, audio-service and shutdown glue to the parent owner.

## Frozen boundary

- `async initialize(&Arc<Core>) -> crate::Result<()>` initializes the parent's
  `OnceLock<Arc<AudioHttp>>` without opening devices or files.
- `handles(&Method, &[String]) -> bool` selects only the seven baseline routes.
- Async `get` and `post` receive `&Core`, decoded path/query or JSON, and the
  original `security::AuthReceipt`; they return `ApiResult<Option<Response>>`.
- `AudioHttp::begin_shutdown` returns clones of one retained close attempt with
  `completed().await`. It joins actual producer/finalization work independently
  of network-body disposal.

GET routes are session phonogram status/WAV and audio status/stream. POST routes
are phonogram frames/start/stop. Frames retain the parent's 16 MiB body limit;
controls retain 64 KiB. No public configuration, capture engine, receiver Tune
requirement, or second native AudioService is introduced.

## Approach

The frozen design keeps uploaded recording separate from native capture. Routing
it through native capture would change the source, require hardware and Tune,
and impose its different duration/rate rules. Keeping one allocation per tiny
uploaded block would bound sample bytes while allowing unbounded metadata.

Use a fixed rolling ring, coalesced immutable recording chunks, and bounded
subscriber queues. This preserves the original observable artifact while
bounding allocation and metadata. Python's actual recorder and HTTP handlers
are the independent oracle; implementation details are not the oracle.

## Compatibility

The session/stream check preserves exact identifiers and validation priority.
Uploads remain 48 kHz normalized samples; derived little-endian PCM16 uses the
original clipping and truncation rule, including -1 mapping to -32767. Original
finite samples, boundary values, numeric conversion behavior and invalid input
errors are checked against Python.

History is exactly five seconds. Start freezes that history as the recording
prefix. The maximum complete recording is 600 seconds, including the prefix;
automatic stop affects recording alone, while the entire valid block still
reaches rolling history and live subscribers. Start/stop/status envelopes,
counts, timestamps, error codes and WAV bytes retain their original meanings.
Nothing is committed to the measurement archive.

PCM streaming keeps `audio/pcm`, `X-Sample-Rate: 48000`,
`X-Sample-Format: pcm16le`, no Content-Length and a 120-second idle limit. Each
subscriber holds at most 64 blocks; normal overflow drops its oldest block and
counts the loss. Disconnection and original credential revocation release its
slot. Empty accepted blocks reset idle time consistently with Python.

## Logical resource limits

The approved defaults are eight lazy recorders, four subscribers per recorder,
64 MiB shared recording/history capacity and 16 MiB shared live PCM capacity.
Recording chunks are at most 64 KiB; the 600-second limit also bounds their
metadata count. At most 32 response/subscription leases exist. WAV responses use
immutable chunk views rather than cloning a complete 57.6 MB recording.
Initial status reads do not allocate a recorder. An additional active recorder
returns `audio_runtime_capacity` before accepting samples; completed artifacts
are not silently evicted to make room for another session.
Each WAV response reserves bounded view metadata as well. Its memory/response
lease follows yielded header/chunk `Bytes`, so a body completing cannot release
the reservation while transport still retains a view.

Allocation reservations precede mutation. A recording-capacity failure is an
explicit resource error. A slow live subscriber never rejects an otherwise
valid upload: global live pressure removes the oldest queued packets, counting
loss for every affected subscriber. If only in-flight data pins capacity, the
new live delivery is counted as dropped. Recording and history still receive
the accepted samples. All queue references and response views share accounted
storage; per-request conversion and chunk metadata are bounded separately.

These are logical application limits, not measured RSS or physical I/O claims.
The parent retains its existing request/body limits. No performance or hardware
acceptance follows from this software implementation.

## Authority and shutdown

Revalidate the original AuthReceipt after session lookup and inside the state
coordinator before accepting mutation. Streaming observes its cached revocation
notification. Uploaded frames require the original session/stream relationship;
they do not require a fabricated RF Tune. Any future participation by native
capture must use the existing AudioService plus original Guard/Tune/actor proof.

The current parent shutdown closes Core before forcing sockets. Waiting for a
backpressured Hyper body to be dropped would deadlock that order. Therefore the
module's close attempt marks the facade closed, acquires the state coordinator
after admitted mutation/finalization, revokes/unregisters queues, and releases
recorder ownership. It does not wait for network leases. Unpolled PCM/WAV bodies
retain only bounded immutable/revocable memory and cached authorization, never
an archive cursor, file, device, Core reference, or capture owner. A later poll
cannot restart production. The parent's eventual socket shutdown drops the
remaining inert memory views.

An uploaded PCM stream needs its peer's write half to remain open, matching the
original Python handler's EOF check. Other finite HTTP responses retain the
listener's existing half-close support. The head gate wraps its already owned
plaintext/TLS transport in `DisconnectIo` for a fully validated, bodyless
`GET .../audio/stream` only. After actual output is flushed, the connection's
normal flush poll arms that same transport's read waker. EOF fails the
connection and disposes the actual subscription even while no PCM arrives.
Unexpected input is retained in one byte and forwarded on the next read.
There is no heartbeat payload, polling timer, extra task, descriptor clone or
second socket owner. The existing path decoder is reused without changing its
validation policy. WebSocket upgrades and other routes pass through unchanged.
Request classification uses the application's parsed `Uri.path()`, including
accepted absolute-form request targets.

## Verification

Use actual Python quantization/status/error oracles, Rust model tests for ring
and recording boundaries, and real loopback Core tests for all seven routes.
Exercise counted slow-client loss, disconnect/revocation, pressure before
acceptance, stable completed WAV views, and default zero device I/O. Hold actual
nonreading PCM/WAV clients while shutdown runs, and latch an admitted mutation
to prove that close joins its real completion without waiting for body Drop.
Use real idle plaintext/TLS peers to prove EOF wakes a pending body, and a
write-half-closed finite GET plus the existing WebSocket 101 gates as controls.
Run focused work first, then the available Core/audio and client checks using
the allocated single-job cache.

The focused commands, using the workspace's pinned toolchains, are:

```sh
CARGO_BUILD_JOBS=1 cargo test --locked -p ori-core --all-features --lib audio_http::tests -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo build --locked -p ori-core --all-features
ORI_CORE_TEST_BINARY=/absolute/selected/debug/ori-core ORI_AUDIO_HTTP_REPORT=/external/report.json python -I crates/ori-core/tests/test_audio_http.py -v
```

The Python gate runs the unchanged original HTTP handler and the actual selected
Core binary under the existing owned process adapter. It fails if the selected
binary is missing, a case fails, any fixture cannot prove child exit zero and
tree JOIN, output exceeds its bound, or the immutable binary changes. Its
external lifecycle JSON records both child outcomes and cleanup failures;
unittest's exit result determines the case outcome. No missing environment is
treated as a skipped pass.

`audio_http_oracle.py --output <fixture> --proof <external-json>` regenerates
the small deterministic original-Python fixture with source hashes. Set
`PYTHONPATH=.` and BLAS thread counts to one when invoking that helper directly.
Execution timestamps belong to the external proof, not the committed fixture.

The software checks exercise the original Python implementation and actual
loopback Core servers on Linux. They cover real plaintext/TLS EOF, nonreading
TCP PCM/WAV peers, finite half-closed HTTP and existing WebSocket 101 controls.
Hosted macOS/Windows execution, native audio devices, Raspberry Pi memory and
latency acceptance remain separate verification; these synthetic uploads make
no hardware or performance claim.
