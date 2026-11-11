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

Buffers are explicit: 64 queued events, 64 decoded events per input chunk,
1 MiB per SSE frame, 40 live sweeps, 16,384 display columns and up to 240 legacy
waterfall rows. Overflow is an error/recovery path, not a silent data drop.
The bounded legacy envelope/threshold display is preserved; authoritative
comparison, occupancy, masks, detection and other analyses remain core API calls.

## Compatibility and toolchain

Both SwiftPM manifests require the Swift 6.4 toolchain. Portable SDK production
sources compile in Swift 6 language mode; streamed measurement and display DTOs
crossing executors have checked `Sendable` conformance. The native production target is also configured for Swift 6 language mode
with complete concurrency checking. Existing XCTest source compatibility remains
Swift 5. The native semantic build and runtime are explicit remaining Xcode CI
gates; configuring the mode alone is not a completed migration claim. The Xcode project registers each
native source once and retains `MACOSX_DEPLOYMENT_TARGET=14.0`. CI selects the
published `xcode-27` runner inventory and verifies Xcode 27.2 / Swift 6.4.2;
`macos-latest` currently defaults to Swift 6.3.3. Existing matrix entries and
hardened action pins remain intact, with an additional fast unsigned native build.

There is no wire-contract change. SDK additions are a committed-event stream,
watermark reader, exact UTC timestamp/display adapters and the default-compatible
`includeMeasurements` snapshot option. Raw measurement strings, integer Hz,
S-meter units and floating-point values remain unchanged. The shared synthetic
`contracts/fixtures/measurement-v1.json` tests microseconds, offset-to-UTC
interpretation, UTF-8 IDs, negative zero and the float64 nextafter bit pattern.
Timestamp canonicalization uses integer proleptic Gregorian arithmetic rather
than Foundation's historical calendar cutover.

## Verification and remaining gates

Implemented behavior is verified with synthetic data. The official Swift 6.4.2
Debian 13 toolchain passed 274 SDK XCTest tests and 16 Swift Testing tests with
zero skips using owned synthetic HTTP fixtures on 2026-11-11. All seven fixtures
stopped and their archives remained unchanged. Without those fixtures, the same eight optional HTTP smoke tests skip
on both baseline `604b206` and this branch; no new SDK skip was introduced.

Portable coverage includes fragmented UTF-8/CRLF SSE, cancellation, bad headers,
size/batch budgets, sequence gaps, duplicate replay, foreign streams, late
archive ordering, raw units, float64 bits, exact UTC, extreme scales, single
points, preservation of narrow peaks, texture wrap, one-row updates and palette
invalidation. Native tests additionally cover real model streaming/recovery,
Pause/Live continuity, late disconnect delivery and Metal pixel orientation.

On Linux, native sources pass Swift syntax parsing, the pure native stream/frame
adapters typecheck in Swift 6 mode, and `tests.test_xcode_sources` passes. Linux
cannot build or run Combine/AppKit/SwiftUI/Metal; no native UI build, GPU pixel
run or application usability result is claimed here. Run the full native gate
on macOS with Xcode 27.2 / Swift 6.4.2:

```sh
python -m tests.run_release_checks --swift --report-dir release-reports
```

That runner owns synthetic fixtures and explicitly skips the physical tinySA
probe. It must pass both Swift suites and the unsigned Xcode build, including
Swift 6 UI migration diagnostics, before this work is called release complete.
The standalone Linux release-runner attempt stopped at its display dependency
gate; the parent integration run owns the full Python/Node/Tk gate.

Physical tinySA/AR-DV1/USB/audio acceptance, eight-hour and autonomous-node runs
remain **not run**. macOS latency, frame-rate and memory targets remain
**unmeasured**; portable correctness tests do not establish those resource gates.
