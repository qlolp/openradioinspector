# Shared modern Web and Windows client

This is an additive client for the existing `/api/v1` Python core. React 19.3.1,
TypeScript 7.0.2, Vite 8.3.8 and Node 26.13.0 build a self-contained SPA. The
Windows shell pins Tauri/API/CLI 2.12.4 and Rust 1.99.1. npm and Cargo lockfiles
are committed. Neither Node nor an Internet connection is needed at user runtime.

## Implemented workflows

* Local Monitoring/Research selection, system/light/dark themes, keyboard focus,
  skip link, modal focus restoration and Alt+M / Alt+R navigation. Switching
  screens never changes the global guard.
* Receive-only project/session wizard: explicit simulated profiles or distinct
  serial ports, conservative tinySA RX limits, antenna/location declarations,
  preflight, explicit collection start/stop, collection progress and confirmed
  baseline. No automatic generator operation or implicit device startup.
* Core-owned guard arm/disarm, broad/narrow/paused/restoring coverage, independent
  tinySA and AR-DV1 status, original S-meter units, explicit analog input selection,
  allowed signal ranges and change watching.
* Detection evidence, disposition/comment review, original sweep and recorded
  audio within two actions from an event row. Audio uses bounded Blob URLs and
  the original sweep can be inspected in 100-point pages or downloaded as JSON.
* UTC/frequency/event-source history filters, 32-item cursor pages, original IDs
  and timestamps, and core-calculated baseline median/p95 comparison.
* Explicit 30-second receiver/analyzer leases, renew/return, receiver tune,
  event-centered narrow search, core maximum and position marks. Expiration
  never silently resumes automation. Analyzer search and receiver evidence use
  their independent session IDs.

Unavailable IQ, network identities, extended ranges and unverified device modes
are labelled. Advanced analytics, task scheduling, export/backup, instrument
diagnostics and larger catalog/event browsing remain available in the legacy UI.
Projects page at 100; the live event list holds 100 and allowed list holds 200.
No new core schema, detection algorithm, hardware owner or archive writer exists
in these clients.

## Streaming and rendering

The browser uses named EventSource events and replay. The native shell uses
reqwest and a Tauri Channel; it does not make cross-origin WebView fetches.
Committed `sweep.received.payload.sweep` and receiver observations are parsed in
a worker and applied directly. A sweep causes no REST aggregate refresh.
Device/session/investigation events invalidate only relevant resources, coalesced
for 500 ms. Monitoring status refreshes every three seconds and the core-owned
event list every five seconds because project updates are not currently emitted
as v1 SSE events. Health is checked every fifteen seconds.

Global sequence gaps, invalid frames, node sequence resets and a full 32-frame
parse queue stop the subscription and explicitly resynchronize from health plus
the latest committed measurements. Snapshot reads have cancellation/generation
guards; old project results cannot overwrite a new selection. Replay duplicates
retain source identity. Missing render history is reported as a client gap, not
invented or interpolated evidence.

Rendering holds at most 256 display rows and 2,048 display points per row. Display
copies preserve the global peak, its first tie, source ID/index and exact UTC
strings; raw archive records never change. The waterfall uses a fixed
1,024×256 RGBA WebGL2 texture and `texSubImage2D` for new rows. A bounded Canvas
fallback stays populated, takes over on context loss, and restores the GPU
texture from the bounded display buffer. It does not upload full history for
each arriving frame.

The visible live spectrum is `canvas.spectrum`. Its non-visible
`data-sweep-id` is acknowledged only after the corresponding canvas draw and a
browser paint opportunity, guarded against superseded generations. An observer
must read this attribute, rather than the source footer or React state, when
measuring rendered-source latency. This is a browser draw acknowledgement, not
an instrument or compositor scan-out timestamp.

## Build and core integration

```sh
cd clients/web
npm ci
npm test
npm run build
```

Copy **all contents** of `clients/web/dist/` into the core's modern asset
directory. Serve its `index.html` at `/modern/` and its hashed assets at
`/modern/assets/*`; redirect `/modern` to `/modern/`. Serve only known files from
that directory with the proper JavaScript/CSS MIME types. Preserve `/` and the
existing legacy routes and `/api/v1`. The SPA currently uses local screen state,
so it does not require arbitrary deep-link rewrites. Do not route API requests
through a new client-owned backend. The existing core owns API authorization,
origin policy and device serialization.

The development Vite proxy uses `http://127.0.0.1:18771` and preserves Host so the
core's same-origin POST check stays effective. Configure the proxy target only
for development; production uses root-relative same-origin `/api/v1` URLs.
There are no CDN fonts, runtime package downloads, analytics or service-worker
caches of archive data or tokens.

## Windows shell

```powershell
npm ci --prefix clients/web
npm ci --prefix clients/windows
npm --prefix clients/web run build:native
cd clients/windows
cargo test --locked
npm run build -- --ci -- --locked
```

Build on Windows with the normal Tauri/MSVC prerequisites. This is a standalone
Cargo workspace so it does not join the candidate core workspace. The build
command produces a separate relative-asset build in `clients/windows/frontend`
from the same React source. NSIS installs per user and embeds the WebView2
offline installer. The shell does not start or package another core service.
The user explicitly connects to an existing HTTP(S) node; remote TLS is configured
on that node.

Only the local `main` window may call six commands: `connect_node`, `api_request`,
`cancel_request`, `start_events`, `stop_events`, `open_legacy`. The last command
opens only the configured origin's `/` in the default browser. No arbitrary URL,
filesystem, shell, hardware or database IPC is exposed. Remote WebView navigation
and new windows are denied; production CSP permits local assets, IPC, worker and
audio Blob URLs. Tokens live in session memory, are not logged or persisted,
and are marked sensitive in the native HTTP client. Redirects, traversal,
credential-bearing node URLs and non-v1 paths are rejected.

The bridge caps JSON/event frames at 8 MiB, audio at 32 MiB and active requests
at 16. HTTP commands have 45-second deadlines; selecting another scope cancels
its native request. Closing/reconnecting cancels the native stream and pending
requests. Reconnection clears previous-node selection and local lease tokens;
an existing core lease remains paused until explicit return by its owner or
disarm. Neither window closure nor client reconnection disarms a running core.

## Verification and limits

The client suite runs on a real temporary SQLite core with explicit receive
simulators, core-generated detection events and synthetic WAV evidence. No
physical devices or production archives are accessed.

```sh
cd clients/web
npm run format:check
npm test
ORI_PYTHON=/path/to/python CHROMIUM_PATH=/path/to/chromium npm run test:e2e
npm run build
npm run build:native
cd ../windows
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
```

Coverage includes guard/leases/tune/search/return, allowed ranges, preparation,
two-action original/audio access, history pagination/comparison/filters,
cancelled stale reads, native connection/scope lifecycle, a deliberately injected
sequence gap, WebGL loss/recovery, offline asset requests, themes/mobile layout,
and 256 committed display rows with no measurement refetches. Browser screenshots
and traces are written under ignored `clients/web/test-results/` and uploaded by
`.github/workflows/modern-clients.yml`.

Portable native tests use actual localhost TCP HTTP/SSE, including split UTF-8
and CRLF framing, cancellation, bearer/no-Origin forwarding, declared response
limits, path restrictions and rejected redirects. Windows-specific build,
per-user install and packaged launch are CI gates; the Linux development host
does not establish Windows installer/runtime acceptance. Signing/distribution,
physical USB/audio tests, novice usability, Pi latency/fps/RSS and the target
three-client/endurance load remain separate acceptance gates. Bundle sizes and
synthetic tests are not Pi performance measurements.
