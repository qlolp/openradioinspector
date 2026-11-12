# Shared Rust core candidate

The Rust core, native tests and required child fixtures are delivered here.
The live Python HTTP suites and comprehensive verification helpers arrive in
stage 14; reproduction commands using those helpers apply after that slice.

`ori-core` is an explicit candidate built around Tokio/Axum, `ori-archive`,
`ori-contracts`, `ori-instruments`, `ori-session`, `ori-guard`, and `ori-runtime`.
Python remains the default.
The candidate has not completed API parity or release/hardware acceptance.
Capabilities report the actual implementation and its remaining gaps; an
unimplemented operation is never proxied to a second Python archive writer.

The HTTP server is pinned to `axum-server 0.9.2`. Its maintainer withdrew 0.9.1
after an unplanned ACME feature was included ([upstream explanation](https://github.com/programatik29/axum-server/pull/208));
no security reason was stated. The pinned TLS feature is unchanged. The published
acceptor, server and shutdown source files match 0.9.1 byte for byte; the final
assembled gate still runs the real TLS, framing and owned-shutdown regressions.

## Safe execution and ownership

Default `--mode shadow` opens neither a writable archive nor a hardware port.
Health/capabilities work, while archive/mutation operations explicitly refuse.
There is no writable Archive constructor hidden behind shadow reads. Use an
explicit private replay directory to exercise writes:

```sh
cargo run --manifest-path crates/ori-core/Cargo.toml -- --mode replay --data-dir /absolute/private-replay --host 127.0.0.1 --port 8770
```

The default archive is `data_dir/sessions.sqlite3`, matching Python LocalCore;
`--database` is an explicit same-directory override only. Supervised operation
requires that exact canonical filename; nested paths, symlinks and hardlinked
archive files are refused so alternate data-directory locks cannot alias a
writer. Replay means a private synthetic/archive
copy selected by the operator, not permission to seize an external live owner.
The core acquires `ori_runtime::lease::WriterLease` before opening SQLite or
running archive startup migration. All canonical mutations use the archive's
single bounded writer and are acknowledged only after its committed result.
Only committed ledger rows are used for SSE/replay. There is no independent UI
writer, direct raw SQL path, raw-data conversion, or background source deletion.

Graceful closure stops mutation admission, drains admitted handlers and the
owned device loops and archive, and releases the writer lease only after
successful closure. A failed device or archive close is fail-closed: an
unconfirmed lease is retained until process exit.
Cancelled archive initialization also retains its lease: the blocking opening
job can outlive its async waiter, so an embedding must restart that process
instead of admitting a second writer after an unconfirmed startup.
The runtime supervisor must still wait for the actual owned process and listener
release. Advisory leases cannot exclude an unmodified legacy program that
ignores them; keep orderly legacy handover and per-device native exclusion.

CLI compatibility currently includes `--data-dir`, `--host`, `--port`,
`--archive-max-bytes` (1 GiB default), `--ready-file`, `--check-ready`, and
`--stdin-lifeline`. A readiness file is published without replacing an existing
owner and is removed only when still owned by this process. The stdin lifeline
uses its own detached standard thread; it does not leave a non-cancellable Tokio
stdin task blocking runtime shutdown. SIGINT/SIGTERM request owned graceful
closure. Plaintext listeners are loopback-only; plain LAN exposure is refused.

## Independent device workers

Session creation/start alone never opens a port. Explicit worker start uses only
the stream's named adapter: `tinysa-sim`, `ardv1-sim`, `tinysa-serial` or
`ardv1-serial`. GET `/api/v1/workers` and `/sessions/{id}/workers`, POST
`/sessions/{id}/workers/start|stop` and `/sessions/{id}/streams/{stream}/start|stop|tune`
retain the worker status and `{tuned,worker}` envelopes. Simulators preserve the
existing Python integer-seeded random sequence and label every source simulated.
They provide no physical RF/device evidence.

Physical serial requires both the optional Cargo `serial` feature and explicit
CLI `--allow-serial`. Without them, native worker start fails before port open.
The trusted Rust embedding boundary `DeviceOptions.transport` permits an explicit
connector; tests inject owned duplex byte streams there. Native transport never
enumerates ports. Before connecting, Core validates the conservative verified
profile and reserves the native port identity; Unix aliases share character-
device identity, and the pinned connector excludes OS peers before configuring
termios. Unmodified legacy owners still require an orderly handover.

Each target has its own lifecycle lock, bounded eight-command queue, actor and
measurement loop. Default capacity is 32 active/pending devices and 128 retained
lifecycle entries; completed entries can be evicted, active owners cannot. Serial
connect and stop have five-second default deadlines. Neither waiting for device
I/O nor stopping one device holds a global/archive coordinator lock. Source rows
and their ledger events commit before counters advance. Archive capacity failure
stops the affected owner; no raw sample is silently replaced or discarded to
claim continued coverage.
Processed traces and zero-span acquisitions are never relabelled as raw
monitoring sweeps; they require the explicit artifact workflow.

Start tickets are captured before bulk-start I/O. Stop invalidates those tickets,
including delayed connects, and session stop keeps new starts closed through its
final archive transaction. Controls carry an opaque cached authorization check
to actual actor execution. A command delayed behind a measurement cannot reuse a
revoked owner or reconnect to a replacement worker. Tune completion checks the
original worker identity and applied-tune revision; an acknowledgement is not a
physical frequency readback. Trusted receiver measurements continue during
manual control ownership until that worker is explicitly stopped.
Native prepare/receiver tune retain the same original execution check at every
read, partial write, flush and completion boundary. Revocation mid-exchange
taints the connection; later commands are refused and trusted close still works.
Applied tune revision advances once at execution, rather than on every poll.

`Core::device_preparation` exposes a cache-only `PrepareReport` after the actual
tinySA safe-prepare acknowledgement. It separates input/OFF command confirmation
from `device_mode_readback:false` and `generator_output_readback:false`. Unknown,
tainted, closed or lab state yields no usable receipt. Cold configuration-only
preflight need not invent such a receipt; actual worker start waits for it before
returning healthy native status. The receipt's timestamp is kept out of raw
sweep settings so it cannot change detector fingerprints between connections.

Quiescing prevents new worker starts and cancels only active owned exchanges;
idle lab connections are closed orderly. Stop waits for the actual owned
transport to drop after the actor's close acknowledgement. Cleanup failure stays
visible and retains both the port reservation and external writer lease. There
is no port-based process killing or automatic reconnect of a tainted connection.
Guard integration must persist opt-in resume intent before requesting this
device drain, using its shared shutdown barrier rather than public disarm.

Fresh native sweep creation uses the instruments library's checked
`legacy_monitoring_settings` projection after actual acquisition. It preserves
arrays, float bits, units and measurement times while matching the existing
Python receive/control settings and fingerprints. Its connection/revision comes
from the actual owned worker. Archived settings are never passed through this
projection. An explicit control-inspect profile performs safe prepare first,
then inspection; the library's independent cold Inspect remains query-only and
does not establish receive readiness. That cold-inspection behavior deliberately
differs from Python's implicit cold preparation.

## Session and Guard domains

The shared Session and Guard libraries own candidate/investigation transitions,
named references, measurement results, signal-list exchange, monitoring projects,
preparation, baselines, allowed signals, analysis, review, settings, alerts,
history, manual leases and search. The HTTP adapter retains the existing paths,
envelopes and typed domain errors. Core alone supplies actual independent worker
effects, storage probes and bounded notification delivery. Reference validation
uses the configured read-only scientific worker; its capability stays false
without that worker.

All domain mutations use the archive's same coordinator for brief state checks
and commits. No device command, child execution, storage probe, webhook, detector
calculation or join runs under that coordinator. Delayed controls retain the
original Guard permit, target epoch and optional expected worker identity through
actual actor execution. Tune completion verifies the original worker and applied
revision from cache; manual takeover or reconnect cannot revive an old receipt.
Each analyzer/receiver has independent ownership, and trusted measurements keep
running during manual receiver control.

Manual investigation start, confirmed-next start and completion retain their
originating authentication and session lifecycle. The two RF operations also
capture the original negative Guard permission and existing worker once. Their
bounded (16) owned continuations retain mutation admission through device JOIN
and the terminal database commit when the HTTP waiter disappears. Original
revocation while waiting for the coordinator cannot commit tuning; revocation
after tuning produces the existing failed/queued terminal state. Archive-only
completion checks canonical evidence without inventing a connected receiver.

The typed tinySA coordinator uses the same outer worker and original per-I/O
authority. Its cold cleanup barrier is installed before Guard recovery can
start an opted-in actor. `hardware_control` describes the bound software API;
`explicit_serial_adapters` and `candidate.native_connector_enabled` separately
report operator connector enablement. Physical validation remains `not_run`.

The internal acquisition-health cache records monotonic analyzer poll and raw
commit phases without querying SQLite or opening a device. It excludes receivers
and ended workers and retains actual pause/Lab state. Only successful raw append
acknowledgements advance completed cycles. Its configuration epoch/time are the
existing confirmed control revision and its actual acknowledgement instant;
they also change for acknowledged control actions beyond timing settings. This
is an observer input, not hardware readiness or a physical resource guarantee.

The Guard stop adapter returns `stopped` only after the device manager confirms
that the original worker's cleanup and join completed. It retains that worker's
instance identity. Public worker snapshots continue to report `disconnected`;
a cached snapshot alone is not a Guard handover receipt.

Startup awaits Guard recovery and any explicitly requested autostart attempt
before reporting `preflight_complete`. Invalid restart metadata is rejected
before consuming resume intent. Recovery never rewrites raw sources or replays
historical jobs. Cold/unconfigured service readiness is separate from actual
per-project baseline/device readiness and physical hardware validation.

The automatic detection, receiver handoff and archive-evidence completion
facade shares this same Session/Archive/device ownership. Policies start disabled
and remain runtime-only. Configuration retains the original request and policy
epoch through its event acknowledgement; each running pass captures the actual
session and any participating worker once. Pure uploaded-sweep detection and
archive-only completion require no invented actor. The fixed Python detector
runs through the shared bounded research worker; RF handoff uses the retained
receiver and its original completion proof.

Shutdown quiesces admission and obtains one shared Guard shutdown handle. It
awaits that handle's durable opt-in intent barrier before cancelling native
polls, then awaits the same cleanup attempt before archive close and writer
release. It never calls public disarm first. Intent/stop/archive failure retains
ownership. Public disarm clears resume intent; controlled service shutdown
preserves it only for explicit autostart. Repeating a failed Core close does not
silently start another cleanup attempt.

Listener shutdown joins its owned cleanup task. It drains admitted mutations
and effect/reader/child joins before terminating any remaining owned sockets.
A client that stops reading an SSE or export body cannot keep the process
alive through network backpressure. HTTP upload and WebSocket ACK deadlines
retain their separate existing budgets.

Storage probes use two bounded filesystem slots retained through the actual
blocking I/O join. Notifications use two bounded network slots, a three-second
deadline, exact-byte HMAC-SHA256 and the notification ID header; redirects are
disabled. Secrets and private filesystem failures are not included in responses.

### Derived history jobs

`CoreConfig.history` supplies optional trusted worker and source/cache budgets.
The CLI inherits `--analysis-python`, fixes argv to `-I -m ori_research`, and
uses the installed worker 0.2.2 protocol. Missing containment or no configured
worker leaves the cold UI available and returns `history_worker_unavailable`
for history calls. `candidate.history_worker_configured` describes configuration,
not a successful scientific run or physical hardware preflight.

`POST /api/v1/history/jobs` accepts the library's closed export/summary/occupancy
request and returns 202 with a job view. GET of `/jobs`, `/jobs/{id}` and
`/jobs/{id}/result` returns bounded views/results. POST `/jobs/{id}/cancel {}`
cancels; POST `/jobs/{id}/pages {"cursor":...}` replays the same sealed source.
GET `/jobs/{id}/exports/{streams|sweeps|sweep_points|observations}` streams a
fixed Parquet table in at most 64 KiB reads. A busy shared research pool causes
bounded asynchronous retries, never a false EOF. Each chunk has a 30-second
admission/read deadline, separate from the HTTP upload and worker deadlines.
Original request revocation or shutdown ends the body. GET
`/api/v1/history/waterfall` accepts the typed viewport fields and a bounded JSON
cursor; the derived cells retain original measurement IDs and point indices.

History and point science use the same single heavy-worker pool. The Core owns
one cooperative background I/O timeline and private ephemeral roots. History
retains its registration through worker/selection cleanup; cancelled shutdown
waiters cannot remove those roots early. Defaults are 256 MiB of selected sealed
sources and 10 GiB combined disposable history storage. Canonical data is not
copied wholesale for a selected job. Shutdown joins History before Archive and
removes the ledger only after its consumers drain. Cross-process timeline
support is reported separately from complete consumer coverage; aggregate
coverage remains false until every file consumer is connected. This is logical
application accounting, not an OS physical I/O or filesystem quota.

Point-analysis source selection, request encoding and child execution also have
an owned completion task. A cancelled HTTP waiter signals its original token
and cannot release the global research reservation or QoS registration before
the admitted read and child/IPC joins complete. Core shutdown drains the same
research reservation before removing its ledger or releasing Archive ownership.
The shared worker wrapper injects a private I/O capability only for fixed trusted
RPC module arguments. It validates and consumes the outer success receipt before
the ordinary five-field response reaches a client; memory-only science must
report zero file-I/O charges. Unexpected, missing or malformed receipts fail
closed. Private paths and capabilities never enter the public result.

The focused integration gate uses a temporary archive, real loopback HTTP and
the installed isolated 0.2.2 worker for all three job types, fixed Parquet bytes,
raw-source preservation, and signed-zero viewport provenance. A separate owned
blocking-thread test cancels a close waiter and verifies that cleanup waits for
the registered owner's actual join. These are synthetic software checks.

### Raw archive delivery

The JSON `/api/v1/sessions/{id}/archive/export` and newline-delimited
`/archive/export.ndjson` routes use `SnapshotExport::open_live`. One read-only
WAL transaction freezes both counts and exact original records. No whole-database
temporary copy or heavy research reservation is held for a raw download. The
Archive's two live-reader slots are shared with derived source selection.
Records retain exact UTC/UTF-8 order, original units/settings and float bits;
sweeps precede observations. NDJSON uses the existing header and per-record
format, with frozen `X-Archive-Sweeps` and `X-Archive-Observations` counts.

Each reader has one pull mailbox, one bounded record, and an owned completion
task. JSON encoding runs behind one bounded blocking encoder; its real completion
is awaited before cleanup. Disconnect, credential revocation, pressure and Core
shutdown cancel the reader. Cleanup retains its strong handle and QoS registration
through the SQL/encoding joins. A cleanup failure is sticky and retains ownership.
The owner watches the originating credential even if the HTTP body is never
polled. Every resumed JSON/NDJSON chunk checks that same receipt after pacing;
revocation cannot emit an already prepared measurement or retain either reader
slot behind an idle body. Synthetic credential tests cover both cases and JSON
separators/tails without changing the frozen measurement data.
Raw export permits at most 16 MiB per source record and a five-second SQLite read
deadline. Transfer pacing uses the common cooperative timeline in at most 8 MiB
charges. This is delivery pacing: native SQLite page/predicate reads and physical
I/O are not fully accounted, as `candidate.raw_export_limits` states. WAL remains
canonical committed storage while a live reader pins it; originals are not pruned.

## Task planning and persistence

The existing `/api/v1/task-scheduler` status/configuration, `/tasks` create/list,
and `/tasks/{name}/cancel` routes use `ori-tasks` with the same envelopes and
private input paths omitted. Names remain exact UTF-8 through percent-encoded
routes. The default book is an owned ephemeral directory, removed only after
the actual scheduler thread has joined. `--task-book FILE` selects an explicit
operator book; records survive restart, while execution remains disabled.

The default scheduler preserves the documented software lifecycle: explicit
enable starts the scheduler and due tasks change their software status. Default
callbacks create no measurements, open no ports and confirm no physical work.
`control_task_planning=true`, `control_task_scheduler=true` and
`control_task_execution=false` distinguish that behavior. Status also reports
`execution_available=false`; restart still requires explicit opt-in.

An embedding may provide `CoreConfig.task_execution` with reviewed producer
callbacks and exact operator mappings. The original enabling credential,
ownership scope and task epoch remain attached to each run. Disable revokes and
joins those runs before its scheduler acknowledgement; shutdown uses the same
owned final-result attempt. A caller timeout does not certify cleanup or attach
an old running record to a fresh credential. The optional callback interface
does not invent a `task_file` interpreter or hardware recipe.

One bounded mailbox and owned thread handle saves outside acquisition loops.
Core retains one shared shutdown attempt through actual thread join and owned
directory cleanup. A failed/cancelled cleanup retains Core writer ownership;
another close call cannot silently turn uncertainty into success.

## LAN TLS and client pairing

LAN operation is opt-in for existing installations. A new LAN installation uses
TLS, trusted Host/Origin authorities and per-device credentials. First provision
an identity locally, with every DNS name/IP clients will actually use:

```sh
ori-core init-lan --data-dir /absolute/private-replay --name ori.local --name 192.168.1.20
ori-core --mode replay --data-dir /absolute/private-replay --host 0.0.0.0 --port 8770 --lan --trusted-host ori.local:8770 --trusted-host 192.168.1.20:8770
ori-core pair --data-dir /absolute/private-replay
```

`init-lan` creates owner-only local certificate/key state and exports the public
installation CA at `.ori-runtime/security/public-ca.pem`. It refuses an existing
identity; re-enrollment/rotation is an explicit operator operation. Install that
public CA through each client's operating-system/browser trust settings and
verify the server identity locally before entering a pairing code. Clients must
keep TLS certificate and hostname verification enabled. The leaf fingerprint is
available locally for identity comparison; native clients may also pin it. The
installation CA signing key is discarded after issuing this server identity.

`pair` opens a single-use window through owner-only Unix operator IPC, checks the
peer UID, and displays a code only on an interactive terminal. Default duration
is 300 seconds and five attempts, with maximums of 300 seconds and ten attempts.
Pairing cannot be enabled through LAN HTTP. Local tools can use
`ori_core::admin::request` over `.ori-runtime/core-admin.sock`. Windows local-core
LAN state remains fail-closed until owner-only DACL/IPC handling is implemented;
Windows/macOS thin clients can pair with the Pi/Linux core.

The frozen public routes return the normal version 1 envelope:

| Route | Request and behavior |
| --- | --- |
| GET `/api/v1/security/status` | Unauthenticated, bounded `{mode,pairing_required}`; legacy mode is `legacy_loopback` and requires no pairing. |
| POST `/api/v1/security/pair` | At most 4 KiB `{pairing_code,label,client:"browser"|"native"}`; browser is the default. Browser returns `{paired:true,credential:"browser_cookie"}`. Native returns `{paired:true,credential:"bearer",token}` once. |
| POST `/api/v1/security/browser-session` | Valid Bearer only, body `{}`; returns `{paired:true,credential:"browser_cookie"}` and a cookie for embedded native web views. |
| POST `/api/v1/security/credentials/current/revoke` | Body `{}`; revokes the current credential. DELETE `/api/v1/security/credentials/current` is equivalent. |

Pairing sets `ori_session` with `Secure; HttpOnly; SameSite=Strict; Path=/api`.
Only hashed device tokens persist. The same credential authenticates HTTP and
SSE; revocation invalidates both forms and closes its active event streams.

Control adapters retain the originating AuthReceipt captured under the same
credential mutex as authorization/revocation, before awaiting the request body.
The receipt exposes only cache-only revalidate and an asynchronous revoked wake;
it contains no token getter, Debug or serialization. Bearer and browser-cookie
contexts for the same credential share one permanent revocation flag. Revoking
another device does not revoke this flag. The bounded in-memory hash/weak
registry stores no new credential material on disk. Fresh authorization failure
also invalidates the originating cache flag.

After a bounded upload, mutation admission rechecks this original receipt.
Server shutdown revokes its lifetime flag from any owned thread. Adapters must
use the revoked wake to cancel silent pending I/O, then wait for their original
worker/transport drain; checking only when bytes arrive is insufficient.
Browser clients use same-origin cookies, including EventSource; credentials are
never placed in query strings or JavaScript storage. Cookie mutations require a
trusted Origin. Native clients keep Bearer credentials in their platform secret
store and disable ordinary HTTP cookie handling to avoid sending both forms.
The browser-session response permits installing only its HttpOnly cookie in an
embedded web view before loading static program HTML. HTML/JS bytes contain no
private session data; all data and event requests remain under protected `/api`.
The shared web assets are embedded from `radioinspector/web/static` during the
Rust build. `/`, `/platform`, `/instrument` and the compiled `/static` allowlist
serve those exact immutable bytes. Runtime file paths are never resolved from
the request. Rebuild the binary when the shared web client changes.

Requests with both Bearer and session cookie, duplicate credentials, untrusted
Host/Origin, or forged forwarded-protocol assertions are refused. TLS is bound to
the actual listener, never a client header. Supervised LAN mode separates the
private plaintext loopback runtime listener (`--control-port`, default 8771) from
the public TLS listener. Plain legacy compatibility has no TLS/authentication;
it must remain loopback-only until an explicit controlled candidate cutover.
`--check-ready --lan` verifies the configured local CA and hostname without
opening another archive writer. It never disables TLS verification.
Anonymous access is limited to exact public pairing/status paths and compiled
GET/HEAD program assets. Percent-encoded fixed API prefixes cannot turn a data
or mutation route into an anonymous bootstrap request.

## HTTP, durability and bounded work

The OpenAPI response is the canonical generated runtime variant exported by
ori-contracts. It reuses the unchanged baseline wire schemas and adds reviewed
security/WebSocket operations and explicit support predicates. The fixed document
has 130 paths and 149 unique operation IDs; declaring a compatibility target does
not claim that its implementation or hardware acceptance has passed. Core no
longer maintains a parallel handwritten schema/route description.

Versioned responses retain the Python
`{contract:"openradioinspector-platform",contract_version:1,data:...}` envelope
and structured error shape. The strict SDK codec is used at untrusted JSON
boundaries and outbound serialization: duplicate keys, nonfinite values,
malformed UTF-8, protocol-key aliases, and loss of arbitrary metadata integers
are not silently accepted. Historical reserved-looking metadata keys are
preserved through the archive's own VarJSON boundary and reopen.

Bodies are bounded before parsing: 64 KiB controls, 16 MiB raw measurement
requests, a whole-body deadline of at most 10 seconds, 16 public request slots,
32 MiB aggregate admitted body bytes, two reserved private control slots, and
eight subscriptions shared by SSE and WebSocket. A 64 KiB HTTP/1 header gate runs after TLS and before
Hyper, compares arbitrarily long decimal Content-Length against the route
budget without big-integer parsing, and returns 413 even beyond u64. It rejects
repeated lengths (including equal values) and every Transfer-Encoding. At most
32 accepted connections per listener can perform handshakes/reads or stream;
handshake and header reads are bounded. Ordinary HTTP responses explicitly close
the connection after one request, matching the legacy HTTP/1.0 server; pipelined
follow-ups are never executed. Successful WebSocket upgrades keep their socket
and use the bounded frame protocol instead. The head gate accepts HTTP 1.0/1.1
only; enabling the server upgrade path does not enable HTTP 2 bypasses.
The body deadline applies to upload reading, not SSE lifetime.
Transfer-Encoding/ambiguous Content-Length is refused;
measurement and private shutdown bodies require Content-Length. Empty legacy
control bodies may omit it. Body reading happens before mutation admission, so
uploads do not hold lifecycle locks. Busy queues return explicit errors.
Measurement batch framing/owner checks precede DTO construction and session
lookup, retaining the existing missing-field/error precedence. Missing session
messages retain the exact requested Unicode identifier; no normalization merges
distinct identifiers. Stop also checks session existence before worker policy.

UTC timestamps retain six microsecond digits, IDs remain exact UTF-8, frequencies
remain integer Hz, levels retain float64 bits, and receiver S-meter counts are
never relabeled dBm. Complete raw batches commit atomically with their events;
foreign scopes, duplicates, invalid units and capacity failures roll back.
SSE subscribes before bounded durable replay, advances by committed sequence,
and recovers notice lag from the ledger rather than blocking acquisition. Large
history is fetched in bounded pages, not retained as a whole-process registry.
Legacy list endpoints refuse a required continuation rather than silently claim
that a truncated list is complete.

The resource ceilings are admission bounds, not measured Pi RSS/latency proof.
The 256 MiB core RSS and responsiveness/performance acceptance gates remain open.

## Binary committed event transport

In replay mode `features.measurement_binary_stream` advertises the shared
codec's exact six-field descriptor: version 1, websocket/protobuf,
`/api/v1/events/ws`, subprotocol `ori.measurements.v1.protobuf`, and maximum
frame size 16 MiB + 64 KiB. Shadow mode reports false. The route requires the actual
offered subprotocol and accepts only optional `after`, a canonical unsigned64
decimal cursor (default 0). Credentials never appear in the URL. TLS, trusted
Host/Origin, native Bearer/browser cookie and revocation match SSE.

Each connection sends exactly one committed event frame, then waits for raw
TEXT containing that exact positive decimal sequence, after the client has
applied its worker/display result. Acknowledgements contain 1–20 ASCII digits,
no whitespace or leading zeros. Only a matching ACK advances the local cursor.
Invalid, duplicate, stale, future or idle ACK closes 1008; oversized controls
close 1009. The absolute ACK deadline is 30 seconds from frame transmission and
Ping/Pong does not extend it. It is separate from the ten-second HTTP upload
deadline. Idle keepalive Ping never skips to a watermark.

Reconnect starts from the client's last applied cursor. The server subscribes
before reading the durable ledger, treats commit notices only as wakeups, and
queries one event at a time. Lag/truncated commit acknowledgments cannot discard
history. Sequences beyond signed 64-bit range are retained exactly as idle cursors rather
than cast/wrapped into SQLite's sequence range. Serialization uses the shared
codec: supported sweep arrays are typed without changing float bits, units,
timestamps, frequency integers or opaque metadata; generic frames remain valid
sources when the codec requires fallback.

Two bounded encoders run away from acquisition executors; connection admission
remains owned through their actual blocking join. Encoded frames have a shared
32 MiB in-flight budget retained through the applied ACK. Overload closes 1013
without advancing the cursor. These bounds do not establish a measured RSS or
throughput result. Shutdown/revocation interrupts both send and ACK waits, and
actual socket drop precedes release of the connection admission guard. Core
waits for those guards before archive close/writer release.

## Read-only scientific worker

The fourteen existing comparison/average/change, adaptive/three-threshold,
context/occupancy/mask, candidate/raster rounding, quality/counter, DVB and map
routes use the installed `radioinspector.research_rpc` worker. Core passes exact
selected archive DTOs and the original request to one owned child; it never
proxies a mutation to a Python writer or opens a Python archive in that child.
The scientific algorithms and their existing validation remain authoritative.

Configure an absolute environment executable, preserving its venv symlink:

```sh
ori-core --mode replay --data-dir /absolute/private-replay --analysis-python /absolute/worker-venv/bin/python --research-cgroup /sys/fs/cgroup/delegated/ori-research
```

Install the root wheel in that isolated environment first. The argv is fixed to
`-I -m radioinspector.research_rpc`; no HTTP parameter selects a command, module,
SQL statement or filename. The child environment is sanitized by `ori-runtime`.
Default timeout is 30 seconds (`--research-deadline-ms`). Hard child memory
requires an actually delegated cgroup. `--research-best-effort` is an explicit
development fallback; it does not establish production resource acceptance.
Capabilities distinguish an unconfigured worker and retain release acceptance
as false. There is no implicit Python installation or package download.

One shared `ResearchPool` reservation spans snapshot selection, request
serialization and owned process execution/reaping; overload returns 503 while
health remains available. Response serialization remains bounded by the IPC and
HTTP budgets after the child reservation completes. The same pool is reserved
for other heavy read-only work. Shutdown
cancels and reaps the owned child before closure; process identity is never
reconstructed from a PID or port. Both IPC documents are bounded to 16 MiB and
responses must match version, operation, correlation ID and result/error shape.
Structured worker errors preserve exact legacy status/code/message, including
expected nonzero error exits. A missing session is passed truthfully as the
private null-session variant, preserving method-validation priority without
inventing an existing DTO.

Exact source selection uses one archive read snapshot, not recent-list limits.
Context includes the actual nearest same-stream UTC/UTF-8 neighbors, including
incompatible ones; the worker performs the scientific rejection. Source arrays
are never sampled or truncated. Existing point limits remain unchanged, including
33-source context and the counter's larger grid. HTTP method limits also remain
unchanged: 8 KiB for the small analysis methods, 256 KiB for maps, and 512 KiB for
mask/rounding JSON artifacts. Imported JSON stays a string. GET comparison and
threshold queries reject duplicate fields, malformed percent escapes and UTF-8.

## Current route coverage and recovery

Implemented initial slice:

* Health, capabilities, archive capacity/status and candidate OpenAPI.
* Session create/list/get/start/stop, exact stream descriptors, source ingestion,
  single/batch sweeps and observations, bounded source reads and scoped originals.
* Archive-wide session analytics, bounded sweep summaries with Python-compatible
  filter-bound snapshot cursors, and immutable receiver WAV segment ingestion.
* Receiver/tinySA artifact summary lists, scoped summary reads and original bytes.
* Candidate/investigation creation, queue/start/complete/fail/cancel/dismiss,
  durable event history, SSE with Last-Event-ID and binary applied-ACK replay.
* Named sweep references with scientific validation, methodology/results,
  signal-list import/export, and monitoring project/preparation/baseline/allowed/
  analyze/review/guard/manual/search/settings/alerts/history routes.
* Startup stream-state reset and interruption of tuning/listening/decoding jobs;
  queued jobs remain queued and are never automatically replayed.
* Atomic session stop plus every open investigation cancellation/candidate return,
  with `session.stopped` before cancellation events. The archive's bounded ACK may
  contain only a prefix; the durable event range and SSE remain complete.
* Authenticated private runtime status/shutdown and owned CLI lifecycle.
* Actual rustls HTTPS, local operator pairing, browser/native credentials,
  trusted Host/Origin checks, embedded-browser exchange and revocation.
* Existing fourteen scientific routes through the bounded installed worker.
* Explicit independent simulator/native workers, owned tune and graceful stop.

Sweep-summary queries allow `stream_id`, `limit` (1–100), `after`, `start`, `end`,
`min_hz` and `max_hz`; unknown/repeated filters are refused. Ordering is descending
UTC time then UTF-8 ID. End is half-open and frequency overlap selects a whole
sweep. The first maximum remains the peak even on ties. An opaque cursor binds
filters and ingestion boundary, so later appends cannot enter its next pages.
Summaries contain no source arrays/settings and the archive decodes one source
at a time under the read budget.

POST `/api/v1/sessions/{session_id}/audio-segments` accepts the frozen 16 MiB
JSON contract: `id`, `stream_id`, positive integer `frequency_hz`, `mode`, UTC
`interval`, `data_base64` and lowercase `sha256`. It requires receiver ownership,
complete mono PCM16 RIFF/WAVE (including extensible PCM), exactly one format/data
chunk, complete optional chunks/padding, and frame duration within one microsecond.
Committed WAV bytes remain unchanged. Artifact and `audio_segment.received`
summary event commit together; stable-ID conflicts return 409 without an event.
Scoped GET `/sessions/{id}/streams/{stream}/instrument/artifacts` returns
`{items:[summary]}`; `/{artifact}` returns its summary and `/{artifact}/content`
returns exact binary content. These reads support receiver audio as well as
tinySA artifacts. Capture from a selected host input uses its separate explicitly
configured audio service and original receiver-tune authority.

The seven uploaded-PCM phonogram/live routes also have their own Core-owned
recorder and bounded network hub. They retain the original 48 kHz mono PCM16
conversion, five-second history and ten-minute recording limit. Recording/history
and live queues have separate 64 MiB/16 MiB budgets, at most eight recorders,
four subscribers per stream and 64 queued blocks. An idle disconnected client
releases its subscription; a slow network body cannot block archive shutdown.
These routes open no audio device and perform no RF action. Their capabilities
do not certify host capture or hardware validation.

Typed instrument operations, automatic policies, software task scheduling and
derived history are bound separately to their existing owned services. Optional
real task producers and native audio require explicit configuration. Full software
parity, combined resource/installation gates and physical acceptance remain open.

## Private runtime integration

`ORI_RUNTIME_CONTROL_TOKEN` and `ORI_RUNTIME_INSTANCE_ID` must be supplied
together. `ORI_RUNTIME_DATA_DIR` must agree with the CLI data directory. Tokens
are process-only and never persisted or logged. GET
`/internal/runtime/v1/status` requires a single constant-time Bearer match from
an actual loopback peer and reports version/instance/PID/ready/preflight_complete/
guard_armed/writer_owned. Query tokens and forwarded peer/protocol assertions
are not trusted.

POST `/internal/runtime/v1/shutdown` requires strict version 1,
`preserve_resume_requested:true`, reason shutdown/cutover/rollback, and optional
boolean `acknowledge_coverage_gap` (default false). It returns 202 through the
server's graceful-response path, closes new admission and drains accepted work.
An armed cutover without acknowledgement returns 409 without changing state.
The ownership check and beginning shutdown use the same brief coordinator as
Guard claims, closing the race with an admitted arm operation. An explicit
acknowledged cutover preserves committed opt-in resume intent through shutdown.
Actual exit and ownership release remain the supervisor's acceptance condition.
There is no kill-by-port, foreign-PID lookup, or assumed-graceful child signal.

## Original-tune analog capture

Native capture is disabled by default. Building the `audio` feature and passing
`--allow-audio` explicitly enables the Core-owned backend; construction performs
no input enumeration or open. Trusted embeddings can supply `AudioOptions` with
a backend and clock. Shadow mode still creates no capture owner.

The existing monitoring-project audio-inputs route reports only inputs that
actually advertise exact mono PCM16 at the Guard's 24 kHz rate, together with
their original names and format ranges. Unsupported inputs do not pass Guard
audio readiness. This describes host input capabilities, not an RF readback or
a successful recording. The additive flags `analog_audio_inputs` and
`guard_original_tune_audio_capture` reflect operator enablement. Remote live
playback and the separate phonogram API remain unavailable.

The Guard capture callback binds its nonoptional original `TuneReply` before
awaiting: receiver instance, applied tune revision, frequency/mode and tune UTC
are retained with the original ownership permit. Each accepted PCM block and
the final result revalidate this same cache-only proof. Retuning, reconnecting,
manual ownership changes or shutdown cancel/discard the recording. No permission
or receipt is renewed after waiting. Duration-to-frame conversion uses ties to
even; WAV bytes retain the accepted original PCM16 prefix without normalization.
Capture starts after both original tune UTC and actual input-open UTC.

Shutdown captures the Guard's same retained intent-preserving attempt before
signalling capture cancellation, then awaits the same `AudioClose` completion.
Completion requires actual stream release and capture-thread join before the
archive and writer lease can close. A cancelled waiter does not detach the
owner, and a cleanup error retains writer ownership.

## Legacy panorama HTTP

Replay now owns a separate bounded LegacyWorkspace. GET /api/state, /api/signals,
/api/coverage, /api/occupancy, /api/efficiency, /api/keywords, /api/measure and
/api/stream plus POST /api/database and /api/threshold retain their plain legacy
JSON/SSE bodies. They do not use the v1 envelope. Cold state is available without
a worker. The stream sends the initial snapshot and latest version changes with
one-second keep-alive comments; it shares the Core's eight subscriber slots.

The trusted configured analysis interpreter is reused with fixed argv
(-I -m radioinspector.legacy_rpc). The same single ResearchPool reservation is
taken before private source preparation and retained through child reap.
The original I/O budget also retains that reservation through the final
canonical write and SQL/snapshot cleanup, after the child has exited. Parent
source-copy/sidecar/threshold payload charges and trusted worker file charges
use the same cooperative timeline. The optional private capability and receipt
never enter the public legacy reply.
HTTP cannot supply an executable, SQL, scratch path or snapshot seal. Set
--legacy-library DIR to constrain explicit source selection; LegacyConfig also
accepts lowering-only source/result/cache limits and trusted embedding overrides.
No PAN is opened by construction. Source selection is explicit, one source file
is exclusively leased, and public database identity remains the original path.

The original Python algorithms run only on a disposable copy. The library
publishes validated state after a successful source-version check and commits
only the derived threshold line zero. Raw rows, metadata, nonzero threshold
lines and schema are preserved. Incomplete/custom mutation-bearing schemas are
refused according to the library policy documented in rust-legacy.md.
Workspace/child/SQL shutdown and private-stage cleanup belong to one retained
attempt, awaited before the archive/writer lease closes.

The flags legacy_workspace_state and legacy_panorama_workspace distinguish cached
state from configured scientific operation. Windows local-core PAN loading
returns explicit 503 until owned scientific process-tree containment is
available; mandatory Windows thin clients still use the paired Pi/Linux core.
Aggregate coverage remains explicitly false: native SQLite predicate/page reads,
raw delivery and other producers have distinct accounting coverage. The shared
cooperative file charges do not establish physical disk enforcement. No physical
I/O or Pi performance gate is claimed.

## Verification and remaining acceptance

Use the integrated sibling crates and Rust 1.99.1:

```sh
cargo test --manifest-path crates/ori-core/Cargo.toml --locked --all-features
cargo test --manifest-path crates/ori-core/Cargo.toml --locked --test cli_candidate
cargo clippy --manifest-path crates/ori-core/Cargo.toml --all-targets --all-features --locked -- -D warnings
cargo fmt --manifest-path crates/ori-core/Cargo.toml --check
```

For the real HTTP/direct-service scientific gate, use the installed root package
for the driver and isolated worker, and set `ORI_CORE_TEST_BINARY` to the freshly
built binary when using the root Cargo workspace:

```sh
ORI_TEST_ANALYSIS_PYTHON=/absolute/worker-venv/bin/python python crates/ori-core/tests/test_scientific_http.py -v
```

This gate checks all fourteen real HTTP results against the existing Python
service and verifies missing-session validation priority and an unchanged ledger.
The separate Rust process-fixture tests verify source selection, precision,
overload, cancellation and protocol failure rather than simulating the math.

The Session/Guard binding was verified on Rust 1.99.1 with all 64 Core tests,
six default-build CLI tests, strict Clippy/fmt, and the two real HTTP oracle
tests using the isolated installed Python 3.15.1 worker. No tests were skipped.
The subsequent WebSocket binding passes all 73 Core tests, the six default-build
CLI tests and strict Clippy/fmt. Nine real TCP/TLS tests include 1,205-event
applied replay, exact large sequences/float bits, malformed ACKs, shared subscriber
overload, credential revocation and the actual 30-second deadline despite Ping.
Those are functional protocol checks; they establish no throughput/Pi claim.
The TaskBook planning/per-I/O follow-up passes all 77 Core tests. It adds actual
HTTP checks for payload-vs-session error precedence, exact Unicode not-found
messages, separate software/measurement capability flags, ephemeral/persistent book ownership and
native prepare revocation after the first identity query. It does not complete
task execution or the remaining compatibility work listed above.

The original-tune Audio binding passes all 83 Core tests with all features,
six default-build CLI tests and strict Clippy/fmt. Six new integration tests use
an actual owned receiver actor and capture thread: exact PCM/WAV/UTC, stale tune
before open, retune during capture, original ownership-epoch revocation, exact
format readiness and writer exclusion through blocked stream destruction.
Separate CLI probes confirm native enablement in Shadow creates no archive and
the default build explicitly refuses `--allow-audio`. Linux native validation
uses `pkg-config` with an installed or user-extracted ALSA SDK, as described in
[the audio library guide](rust-audio.md). No audio hardware was enumerated/opened.

The Legacy follow-up passes all 87 Core tests with all features, strict Clippy
and fmt. Four localhost tests cover cold plain JSON/errors, initial SSE,
full-envelope/cache transport and global-pool ownership through client
cancellation and actual child reap. The separate installed Python 3.15.1 HTTP
oracle covers all nine legacy actions and threshold success/error sequences,
exact f64 bits, original Unicode source paths, raw tables/nonzero threshold
lines/schema preservation and graceful Core exit. Its initial response-identity
failure is retained as evidence of the corrected full-envelope handoff. The
Windows fixture requires explicit worker unavailability and an unchanged ledger;
Linux verification does not claim that Windows execution was run locally.

The AuthReceipt follow-up passes four new coordinated tests and all 46 existing
HTTP/TLS/WebSocket tests, strict all-target/all-feature Clippy and fmt. Its real
TLS partial-upload test waits for original authorization, revokes the credential,
completes the body and requires 401 plus no session mutation. Other tests verify
cache-only access with credential storage offline, per-device revocation and
an owned non-runtime shutdown thread. Windows explicitly requires verified
owner-only ACL support for LAN state; loopback receipt cancellation remains
tested independently.

The canonical OpenAPI binding passes the exact-document HTTP regression and
existing scientific-budget, pairing and domain documentation tests, strict
Clippy/fmt, and the independent actual-binary runtime contract probe. The probe
checks shared-document equality, security status, explicit loopback pairing
refusal, binary descriptor, task execution refusal, exact negative-zero summary
and graceful owned process join. Its authentication/hardware-positive flags remain
false; separate real TLS tests provide the authentication coverage above.

Tests use owned local HTTP servers/processes, duplex byte streams and temporary SQLite only. They
exercise default-shadow byte preservation, writer-before-DB exclusion, strict
framing/deadlines, raw precision/metadata reopen, transactional batch rollback,
committed SSE replay, startup interruption without stale job replay, full stop
cancellation, retained restart intent, ready-file ownership and stdin closure.
Device tests additionally exercise queued revocation, delayed-connect stop,
independent measurement progress, actual transport-drop completion, stale tune
receipts, explicit native enablement and failed-cleanup writer retention.
Domain integration tests exercise real HTTP/SQLite and simulator tune/evidence,
cold UI/preflight without fake readiness, and armed cutover acknowledgement plus
durable resume intent before writer reacquisition. An owned HTTP sink checks the
published HMAC vector, exact notification bytes and refused redirects.
Verified TLS tests use temporary private identities, a trusted synthetic CA,
real localhost sockets, and negative untrusted-server checks. They do not
establish physical device, Pi throughput, client usability, operational LAN
trust enrollment, or release cutover acceptance. Those gates and complete API parity
must pass before the Rust candidate can replace the default core.
