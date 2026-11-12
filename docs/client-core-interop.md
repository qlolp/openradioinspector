# Real Core and client interoperability

This functional gate starts a disposable Rust Core, creates an unarmed project,
and opens its embedded WebUI in an owned Chromium instance. It sends 1,205
synthetic sweeps through the actual batch API. No HTTP responses or WebSocket
frames are substituted. A deliberate disconnect checks replay from the applied
cursor. The Worker/display consumer preserves all source IDs and exact f64
values, including negative zero and the adjacent value above 1. UTC and units
are compared with the existing Python input contract, which converts offsets
to canonical UTC. Raw input and expected producer output are retained separately.

The optional Swift executable uses the real native SDK and URLSession. It
replays the same 1,205 sources, then reconnects at its last applied cursor.
An additional sentinel frame proves the server received the preceding ACK:
the production server sends only one outstanding frame. Source IDs containing
NFC/NFD Unicode are compared by exact UTF-8 bytes. The sentinels are also
synthetic committed measurements and are included in the report's count.

The modern release profile runs this gate against the debug Core produced by its
locked workspace tests, before its private Cargo target is removed. Linux and
Windows require Web; macOS requires Web plus Swift replay and resume. The compact
`modern-core-client-interop.json` must prove all 1,205 sources, binary negotiation,
ACKs after application, forced reconnect, immutable executable, and joined Core.
It is a functional command with no invented framework test count. A missing tool,
failed build, partial proof, or unexpected renderer fails the profile.

Prepare the locked dependencies and owned Chromium before running. Build and
package the modern assets before compiling Core so its embedded assets match the
client sources:

```sh
npm --prefix clients/web run build
python tools/build_modern_web.py
cargo build -p ori-core --locked --offline
(cd clients/web && npx --no-install playwright install --with-deps chromium)
python -m tools.modernization.client_interop \
  --candidate /absolute/path/to/immutable/ori-core \
  --node /absolute/path/to/node \
  --renderer canvas \
  --swift-builder /absolute/path/to/swift \
  --report /absolute/path/to/modern-core-client-interop.json \
  --artifacts /absolute/path/to/fresh-artifacts
```

The binary must advertise the supported stream capability. An older artifact
is an explicit failure. Every real WebSocket must negotiate
`ori.measurements.v1.protobuf` and deliver all source frames as ArrayBuffers.
Chromium defaults to the executable supplied by the locked Playwright package;
`CHROMIUM_PATH` can select an explicitly prepared executable. CI records that
actual package executable on every OS rather than assuming a Linux system path.
Swift is optional for standalone runs and is reported as not requested when
omitted. `--swift-builder` requires Swift 6.4.2, builds with one job in private
scratch before starting Core, and queries `swift build --show-bin-path` to locate
the owned product on either platform. The existing `--swift-client` option still
accepts a prebuilt helper; the two options are mutually exclusive.
The default source count is 1,205; `--count` accepts 1–1,205 for focused diagnosis.
The artifacts directory must not already exist. It retains vectors, original
request bodies, browser diagnostics/screenshots and SDK results on failure.
The modern profile retains the compact proof and, on failure, up to 16 KiB from
the owned child log in its top-level stage log; large SDK vectors and private
build trees are not copied into release reports.
Command processes use the existing release runner's owned process cleanup.
Core receives an EOF lifeline, drains and joins, and its executable checksum
must remain unchanged. No serial/audio device, installed service or live
archive is used. This does not establish Pi throughput or physical acceptance.

## Functional rendering profile

The modern profile explicitly selects `--renderer canvas`. Chromium receives
`--disable-webgl --disable-gpu`; the real application's spectrum and waterfall
must both report `Canvas fallback`. The harness records those actual labels and
rejects an unexpected renderer or a fallback caused by graphics context loss.
It does not replace `getContext`, patch rendering, substitute transport data, or
switch renderer after a failure. The same 1,205 sources, applied cursor/ACK
checks, forced reconnect, and 30-second functional wait remain required.

This selection follows a 350-source diagnostic comparison against the same
immutable Core, viewport, source vectors, and real consumer. Forced software
WebGL2 reported `WebGL2` / `WebGL2 · incremental rows`; its median receive cadence
for sources 256–350 was 27.6 ms, versus 11.9 ms with both actual Canvas fallback
states. Worker-post-to-callback medians were 4.8 ms and 1.4 ms; consumer application
was below the timer resolution in both. Both diagnostic runs preserved every
source and passed the ACK/reconnect assertions. These bounded host observations
explain the functional profile choice; they are not a hardware benchmark or a
new acceptance threshold. A separate trace confirmed TCP_NODELAY succeeded on
the owned Core connections.

Standalone `--renderer webgl2` requires both actual WebGL2 states. It uses the
explicit test-only hardware identity fixture and records the underlying driver
separately, preserving real shader and graphics-context loss/recovery coverage.
The production WebUI uses WebGL2 for hardware or unknown renderer identities and
Canvas for an identified SwiftShader context; see [the rendering policy](web-renderer-policy.md).
The browser matrix also tests automatic Canvas selection without that fixture.
Reports explicitly set performance and hardware acceptance to `not_run`; GPU
frame-rate and Raspberry Pi acceptance remain separate target-hardware work.

The regression exposed Nagle/delayed-ACK latency when sending individual small
measurement frames. All three Core listeners now enable TCP_NODELAY before
HTTP or TLS processing. The framing limits, one-frame admission rule, 30-second
ACK deadline and consumer-applied cursor semantics remain unchanged.
