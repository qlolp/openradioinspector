# Native macOS modernization

This slice keeps the native SwiftUI app and macOS 14 deployment target. The core
continues to own hardware and archive writes. No new native shared-core flow
opens a serial port, starts RF generation, or runs scientific analyses locally.
The existing direct-device diagnostic code remains available in its original
separate views; the shared-core app does not instantiate those models.

## Implemented

- The local monitoring/research selection persists in this client's preferences.
  A single monitoring model survives workspace changes. History and Pause view
  do not disarm the project, stop workers, or stop recording.
- Remote nodes and separately supervised local services outlive this client.
  The bundled Python compatibility helper has a different lifetime: its existing
  stdin lifeline ends when the app process exits. The launcher explicitly marks
  this helper and clears an inherited marker in remote mode. Native information
  distinguishes that helper from an autonomous core service; selecting another
  node removes the helper label. No service is installed or promoted by the UI.
- Monitoring leads with spectrum, waterfall, raw AR-DV1 observations and separate
  core device, coverage, archive and delivery states. Research retains every
  existing analysis pane, workflows, exports, manual controls and search.
  Event context opens bounded history; existing event WAV playback and source
  links remain available.
- `ContentView` is a small native navigation shell. Session orchestration,
  workflow actions, reference/comparison actions, monitoring state, monitoring
  controls, charts, display preparation and event lifecycle have separate files.
- Initial loading reads `/api/v1/health`'s `data.last_event_id` before fetching a
  seed snapshot, then consumes `/api/v1/events?after=cursor`. Replay deduplicates
  exact UTF-8 IDs. The global cursor includes foreign-session/metadata events;
  only selected streams update this client's display. Late commits preserve the
  archive's UTC start-time / raw UTF-8 ID ordering. `PlatformExactID` supplies
  byte-based keys for project/event maps, deduplication, SwiftUI lists and
  picker selections. Canonically equivalent IDs such as `é` and `e\u0301`
  remain distinct; the original String remains the value sent to the API.
- While subscribed, periodic snapshots refresh metadata without fetching sweeps
  or observations. Missing SSE support retains REST polling. Sequence gaps and
  malformed delivery reseed from committed archive data. Buffer overrun leaves a
  visible notice and switches to snapshot polling until UI reconnection.
  Cancelling or closing the client closes its HTTP subscription only.
- SSE framing and decoding run on a serial URLSession delegate queue. Bounded
  display validation, envelopes, meshes, palette conversion and fallback bitmap
  preparation run outside the main actor. Request generations and cancellation
  prevent superseded results from replacing a newer frame or local scale.
- Metal spectrum rendering preserves every point, peak position, threshold and
  scale. Waterfall textures use a row ring and upload only appended rows;
  scale/grid changes or skipped generations rebuild the bounded texture.
  Each renderer permits one GPU command in flight, avoiding CPU writes into active texture
  storage. Unsupported Metal/pipeline creation uses native Canvas/bitmap views.
  Set `ORI_NATIVE_RENDERER=canvas` locally to exercise that fallback.
- Native connection preflight consumes the additive security status/pair/revoke
  APIs. Pairing submits one explicit code during an operator-opened core window;
  the app does not open that window. HTTPS uses system trust and the operator's
  installed CA, with no certificate bypass or plaintext fallback. A 404 status
  endpoint requires explicit legacy confirmation for this UI connection.
- Bearer credentials live in the macOS login Keychain, scoped to the normalized
  node origin/base path. A separate actor performs Security calls; no token is
  copied into preferences, logs, URLs or JavaScript. Credentials have redacted
  descriptions/reflection. The login Keychain supports the unsigned app without
  entitlement-dependent fallback; entries do not synchronize. Access failure
  stops pairing rather than retaining a memory-only credential. Revocation
  matches a credential fingerprint atomically so an old window cannot erase a
  newer window's saved credential. Explicit forgetting is local; UI close does
  neither revocation nor disarm.
- One authenticated SDK client is shared by monitoring, research, metadata,
  SSE, bounded audio reads and archive exports. Native cookie processing is
  disabled. Opening the lab explicitly exchanges Bearer for the core's bounded
  browser-session response and validates its single host-only Secure/HttpOnly/
  SameSite=Strict `/api` cookie. The cookie is installed in a nonpersistent
  WebKit store before public program HTML loads. Closing/superseding preparation
  cannot navigate later. Only an accepted loopback or explicitly confirmed
  legacy connection can omit this exchange. A revoked Bearer disconnects UI and
  requests pairing again without changing core worker/project state.

Buffers are explicit: 64 queued events, 64 decoded events per input chunk,
1 MiB per SSE frame, 40 live sweeps, 16,384 display columns and up to 240 legacy
waterfall rows. Overflow is an error/recovery path, not a silent data drop.
The bounded legacy envelope/threshold display is preserved; authoritative
comparison, occupancy, masks, detection and other analyses remain core API calls.

## Compatibility and toolchain

Both SwiftPM manifests require the Swift 6.4 toolchain. Portable SDK production
sources compile in Swift 6 language mode; streamed measurement and display DTOs
crossing executors have checked `Sendable` conformance. The native production
target also uses Swift 6 language mode with complete concurrency checking.
Existing XCTest source compatibility remains Swift 5. The production native
semantic build and runtime tests passed in Xcode CI at `4a462f3`. The subsequent
pairing slice passed its targeted native tests and production build at `5f2395d`;
the complete updated native gate remains required. The project registers each
native source once and retains `MACOSX_DEPLOYMENT_TARGET=14.0`. CI selects the
published `xcode-27` runner inventory and verifies Xcode 27.2 / Swift 6.4.2;
`macos-latest` currently defaults to Swift 6.3.3. Existing matrix entries and
hardened action pins remain intact, with an additional fast unsigned native build.

The measurement contract is unchanged. SDK additions are a committed-event stream,
watermark reader, exact UTC timestamp/display adapters and the default-compatible
`includeMeasurements` snapshot option, plus an optional immutable Bearer
credential and consumers for the frozen additive security APIs. Raw measurement strings, integer Hz,
S-meter units and floating-point values remain unchanged. The shared synthetic
`contracts/fixtures/measurement-v1.json` tests microseconds, offset-to-UTC
interpretation, UTF-8 IDs, negative zero and the float64 nextafter bit pattern.
Timestamp canonicalization uses integer proleptic Gregorian arithmetic rather
than Foundation's historical calendar cutover.

## Verification and remaining gates

Portable behavior is verified with synthetic data. The official Swift 6.4.2
Debian 13 toolchain passed 282 SDK XCTest tests and 16 Swift Testing tests with
zero skips using owned synthetic HTTP fixtures on 2026-11-11. All seven fixtures
stopped and their archives remained unchanged. Without those fixtures, the same eight optional HTTP smoke tests skip
on both baseline `604b206` and this branch; no new SDK skip was introduced.

Portable coverage includes fragmented UTF-8/CRLF SSE, cancellation, bad headers,
size/batch budgets, sequence gaps, duplicate replay, foreign streams, late
archive ordering, raw units, float64 bits, exact UTC, extreme scales, single
points, preservation of narrow peaks, texture wrap, one-row updates and palette
invalidation. Native tests additionally cover real model streaming/recovery,
Pause/Live continuity, late disconnect delivery and Metal pixel orientation.
The new portable security tests cover explicit/bounded pairing, response
validation/redaction, Bearer reuse, cookie isolation, unsafe cookie rejection
and a simulated certificate failure without plaintext retry.

At `4a462f3`, [native CI job 130402051279](https://github.com/qlolp/openradioinspector/actions/runs/42984380635/job/130402051279)
passed the full macOS gate: 1,753 Python/Node/Tk tests with zero skips, the
10,045-sweep synthetic seven-day scenario, 277 SDK XCTest plus 16 Swift Testing
tests, 153 native XCTest plus four Swift Testing tests, and the unsigned Xcode
build. All seven fixture archives were unchanged and stopped. The Metal API
pixel path passed; the bitmap-fallback branch was not taken on that host.
No Swift compiler warnings were reported in that run.

At `5f2395d`, [targeted native CI](https://github.com/qlolp/openradioinspector/actions/runs/42991219752/job/130424311971)
passed 12 SDK regressions, all 14 new native security tests, both launcher tests
and the unsigned Swift 6 app build; macOS/Windows packaging also passed.
The owned login-Keychain test passed in 0.078 seconds, and the nonpersistent
WebKit cookie test in 0.190 seconds. The separate full release job passed all
285 SDK XCTest tests, then timed out at the existing 600-second native-command
deadline. Its native build took 59 seconds; buffered output stops during the
native suite. This is an unresolved failing gate, not a complete native pass.
At `67cdb74`, the held-revocation regression, awaited WebKit cookie deletion and
all 15 targeted native security tests passed, followed by the Swift 6 app build
with no Swift compiler warnings. Its full run again reached the unchanged
600-second deadline. The owned three-second sample identifies
`NativeKeychainCredentialStore.save` → `SecItemAdd_osx` → `defaultKeychainUI` →
`makeLoginAuthUI`: Security is waiting to create a default login Keychain under
the runner's empty test HOME. This is distinct from the normal login context in
which the targeted tests passed. Only the `swift-native` subprocess now restores
the original login HOME (or preserves its original absence), after its owned
fixture URLs are populated. Python/Node/Tk, fixture servers, SDK and Xcode retain
the empty private HOME; native temporary paths remain private. A runner
regression covers both HOME-present/absent cases, fixture URLs, cleanup and
unchanged parent environment. The real Keychain test and the 600-second
deadline remain in force; the corrected full run is pending CI.

Keychain tests inject Security calls and also
exercise one random, owned synthetic account in the login Keychain, removing
it afterwards. They do not read existing credentials or change Keychain
search lists, lock state or access controls. A signed-app/login-Keychain acceptance
and an operator-installed-CA connection remain separate manual gates.

On Linux, native sources pass Swift syntax parsing, the pure native stream/frame
adapters typecheck in Swift 6 mode, and `tests.test_xcode_sources` passes. Linux
cannot build or run Combine/AppKit/SwiftUI/Metal; local parsing does not verify
new native code or application usability. Run the full native gate
on macOS with Xcode 27.2 / Swift 6.4.2:

```sh
python -m tests.run_release_checks --swift --report-dir release-reports
```

That runner owns synthetic fixtures and explicitly skips the physical tinySA
probe. It must pass both Swift suites and the unsigned Xcode build, including
Swift 6 UI migration diagnostics, before this work is called release complete.
The first standalone Linux release-runner attempt stopped at its display
dependency gate. A subsequent attempt with the owned display ran all 1,753
Python tests with zero skips but failed five timing-sensitive checks (one
failure, four errors). Focused reruns of all four affected methods passed,
including instrumented fixture startup measurements of 0.201–0.252 seconds;
the cause of the full-run failures is not established. The parent owns the
quiet integrated Python/Node/Tk gate. Neither failed attempt is a passing gate.

`python macos/ci/test_launcher.py -v` runs the shipped shell launcher with owned
fake UI/core executables. It verifies that remote mode starts no local process
and clears an inherited helper marker, while the bundled mode sets its marker,
closes its lifeline and removes its private runtime directory on UI exit. It
uses no hardware, archive or installed service.

The Darwin CI wrapper samples only a live XCTest descendant of its known
release-check child after 60 seconds, rechecking PID ancestry/start identity.
At most eight bounded samples go into the existing diagnostic artifact.
The release runner's deadline, checks and process cleanup remain in force.
`python macos/ci/test_native_ci.py -v` verifies ownership selection without
starting subprocesses or touching devices.

Physical tinySA/AR-DV1/USB/audio acceptance, eight-hour and autonomous-node runs
remain **not run**. macOS latency, frame-rate and memory targets remain
**unmeasured**; portable correctness tests do not establish those resource gates.
