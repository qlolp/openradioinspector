# Owned runtime and offline service migration

`crates/ori-runtime` is a standalone Rust 2024 library and foreground
`ori-supervisor` executable. The primary deployment is Pi/Linux. Nothing in
`deploy/` installs or starts itself. The legacy Python default remains available;
these templates require the backend IPC integration described below before use.

Implemented and verified with synthetic child processes, temporary files,
loopback sockets, and temporary TLS keys on Linux x86_64. This establishes no
Pi performance, radio, USB/audio, physical endurance, Windows service, or macOS
local-core acceptance. Windows/macOS clients can remain thin authenticated
clients of the Pi. No raw database table, recovery bundle, task record, or source
measurement is modified by this runtime.

## Ownership and backend contract

`InstanceLease::acquire(data_dir)` exclusively holds
`.ori-runtime/instance.lock` for the supervisor lifetime. Every backend,
including a directly launched participating core, must hold
`WriterLease::acquire(data_dir)` on `.ori-runtime/writer.lock` before opening any
instrument or canonical writer, and keep it through hardware/archive closure.
The supervisor checks this actual OS lock as well as the readiness response.
Locks are cooperative advisory locks; retaining an open lock file matters, not
its existence. Never delete lock files to bypass ownership.

Rust leases explicitly release their OS lock before closing the descriptor.
On Unix, `CLOEXEC` alone does not stop an unrelated concurrent child from
retaining a fork-inherited probe lock until it reaches `exec`. A deterministic
Linux test holds a real owned child before `exec`, drops the writer probe, then
starts the synthetic HTTP backend while that child still waits. Live leases
continue to exclude other owners; startup still checks the writer and listeners.
Both synthetic backend fixtures use the runner's absolute `ORI_TEST_PYTHON`
when configured, preserving the absolute local system fallback. The fork test
also enables startup milestones capped at 12 records, 1 KiB per record and 4 KiB
per launch. Failure reports read a bounded trace only after confirmed child
cleanup. Missing evidence is reported explicitly and does not identify a cause.
This test instrumentation does not establish the cause of a native readiness
timeout or change the full startup checks and two-second fixture deadlines.

A supervisor launches an **absolute executable and argument vector** with no
shell. It creates fresh 256-bit opaque `ORI_RUNTIME_CONTROL_TOKEN` and
`ORI_RUNTIME_INSTANCE_ID` values and sets `ORI_RUNTIME_DATA_DIR`. The token is
never persisted or logged; a status response may contain the instance ID and
owned PID. The backend must not log the token or its environment. Child stdout
and stderr are discarded by the supervisor; the core may maintain its own
bounded, redacted diagnostics. No PID from a file, discovered port, or foreign
process list is ever used to terminate another process.

The private loopback control listener authenticates **every** request with
`Authorization: Bearer <ORI_RUNTIME_CONTROL_TOKEN>` using constant-time
comparison, including status. It must never publish this credential to the
public Web API or serve the private routes on a LAN listener. Readiness and
shutdown responses have a bounded body and `Content-Length`; connections close
after a response (no chunked responses in protocol v1).

* `GET /internal/runtime/v1/status` returns HTTP 200 and
  `{version:1, instance_id, pid, ready, preflight_complete, guard_armed,
  writer_owned}`. `ready` means the API can safely serve clients, including a
  cold unconfigured node. `preflight_complete` means startup recovery,
  migration and service-safety checks completed. It **does not** claim that
  instruments are configured or that a project passed its actual hardware
  preflight. Guard arming still requires that real per-project preflight.
* `POST /internal/runtime/v1/shutdown` accepts
  `{version:1, preserve_resume_requested:true,
  reason:"shutdown"|"cutover"|"rollback", acknowledge_coverage_gap:false}`.
  Atomically reject a cutover while armed unless acknowledgment is true, stop
  accepting new acquisition commands, drain/commit accepted work, close the
  monitoring service/archive/instruments, and exit. Return HTTP 202 promptly
  after accepting; actual exit is checked separately. Preserve opt-in
  `resume_requested` exactly. **Shutdown is not disarm.** Never replay stale jobs
  or bypass startup recovery/preflight on the next launch.

The loopback request deadline is 500 ms and response cap 32 KiB. Backend startup
and graceful shutdown deadlines are configurable; templates allow 120 seconds.
The control handler should acknowledge immediately and run closure outside the
request handler. A malformed/wrong-identity/unready backend does not activate.

Unmodified legacy programs do not take these locks. The supervisor refuses an
occupied configured listener or writer lease and asks for orderly handover; it
cannot claim exclusion against an external program that opens a different port
and ignores the lease. Stop that legacy process through its existing graceful
path first. Do not kill by port, delete its locks, or forcibly seize its devices.

## Explicit cutover, rollback and service commands

Library entry points are `Supervisor::{new,start,status,cutover,rollback,shutdown}`.
`CutoverOptions` distinguishes automatic switching from explicit operations.
Automatic switching is always refused while armed. Explicit switching while
armed requires `acknowledge_coverage_gap:true`; the core rechecks this atomically
when accepting shutdown. The runtime never calls disarm to get around this rule.

Cutover validates the new specification, records a possible coverage interruption
in the fsynced private `coverage.ndjson`, quiesces and gracefully stops the old
owner, waits for its actual process exit and configured listeners/writer lease
to release, then starts and probes the candidate. An unready or failed candidate
is completely stopped before the previous executable is restarted. Linux/Unix
children receive a new process group; an unreaped owned leader reserves its
PID/PGID until owned group cleanup. Descendants must not detach their process
group/session. The runtime does not signal a foreign process or assume SIGTERM
is graceful for the legacy core. A graceful-stop refusal/timeout leaves the old
owner in place and never starts the candidate.

Rollback uses the **latest committed data**, not an archive backup from before
the switch. A failed rollback remains an explicit error/outage with an open
coverage interruption; it never authorizes a second writer. Core startup must
mark interrupted investigations/coverage correctly using the core's existing
recovery logic. Release compatibility checks must reject a candidate that makes
the prior release unable to read its committed data; this runtime does not
invent a schema downgrade or rewrite raw tables.

Unix service administration uses the owner-only
`.ori-runtime/supervisor.sock` (also verifies peer UID), with bounded JSON
requests. The CLI supports:

```sh
ori-supervisor run /etc/openradioinspector/supervisor.json
ori-supervisor status /var/lib/openradioinspector
ori-supervisor cutover /var/lib/openradioinspector /path/to/candidate.json --acknowledge-coverage-gap
ori-supervisor shutdown /var/lib/openradioinspector
```

A candidate JSON file is a `BackendSpec`, the `backend` object from
`deploy/supervisor.example.json`. The acknowledgment flag is optional for an
unarmed guard. Successful selections are atomically retained privately across
restarts. A persistence failure triggers rollback; an existing unsafe selection
file is never overwritten or deleted to hide the fault. `SIGINT` and `SIGTERM`
to the Unix supervisor trigger authenticated graceful shutdown. A manager's
ultimate timeout/forced kill remains an abnormal interruption and must be
reported during core recovery.

`deploy/linux/ori-supervisor.service` is an offline system service template with
an enclosing 1.5 GiB memory budget, no swap, owner-only umask, acquisition priority
and controller delegation. Provision the account, dialout permissions, SSD data
directory and offline release first; render paths and validate the unit before
operator installation. It does not configure devices or enable monitoring.

`deploy/macos/...plist` is an opt-in **per-user LaunchAgent**, with literal paths
that must be rendered. A native client can instead embed the supervisor library.
LaunchAgent priority hints do not establish hard RSS, I/O or storage budgets.

`deploy/windows/register-user-task.ps1` is an explicit per-user interactive logon
task template, not an SCM service. The current Windows supervisor only owns its
direct Child handle. It lacks Job Object tree containment and verified private
DACL persistence. Windows LAN credential/TLS-state creation and isolated research
therefore fail closed in this build. Use the Pi for the local core, or an embedded
Windows lifecycle integration with an externally verified containment/ACL layer.
`Stop-ScheduledTask`, task timeouts and logout are not graceful cutover mechanisms;
no Windows hard-limit/service acceptance is claimed.

## Research child and real resource capabilities

Use one shared `ResearchPool` across the service. `try_reserve()` admits one heavy
job before source selection or sealed-snapshot preparation and returns `Busy`
immediately under overload. Its non-cloneable reservation is consumed by
`ResearchPermit::run`; existing `ResearchPool::run` uses it internally.
`Cancellation` and deadlines
terminate and reap the owned process group, retaining the sealed source. The
configured executable receives one bounded version-1 JSON request (<=65,536
bytes) then stdin EOF. stdout is one JSON response (<=2 MiB); stderr <=64 KiB
is discarded and never echoed into API errors. The response version/request ID,
exit status and JSON framing are checked. Commands never contain shell strings.
See the research worker contract for sealed-source verification and operations.

The runtime injects these maximum limits; a request may lower them:

| Resource | Default ceiling |
| --- | --- |
| Heavy children per shared pool | 1 |
| Child/process group RSS | 768 MiB |
| DuckDB internal memory / threads | 512 MiB / 2 |
| Derived cache / temporary bytes | 10 GiB each |
| Background I/O | 20 MiB/s |
| Entire service background memory | 1.5 GiB via the Linux service manager |

`WorkerSpec.require_hard_memory` defaults to true. With no delegated Linux
cgroup the call refuses to launch unless best effort is explicitly selected.
On Linux an explicitly supplied **empty dedicated cgroup v2 leaf** is verified
on cgroupfs, configured with `memory.max`, `memory.swap.max=0`, `pids.max=32`,
low CPU/I/O weights, and entered before exec. Supplying the actual SSD block
device(s) sets aggregate `io.max` read+write rate to at most 20 MiB/s, divided
across the listed devices. Operators must include all devices the job can touch.
Lowered request I/O limits remain additionally cooperatively enforced by the
research worker. Nice/idle I/O scheduling alone is never described as a byte cap.
Controller delegation and cgroup preparation require installation policy; this
library does not elevate or modify an unrelated cgroup. Provision a controller
hierarchy with the supervisor in a separate control leaf before enabling memory
controllers for its research leaf. An already populated leaf is refused.

The returned `EnforcementCapabilities` states process-tree termination, hard
memory, hard I/O, sampled RSS, hard cache quota and hard temporary quota
separately. Cache/temp hard quotas are **false** here: the worker applies its
cooperative byte checks, while the operator must provision separate filesystem
project/volume quotas for its private cache and `.temporary` subtree (10 GiB
each). The library cannot verify such quotas and never claims it did. Enabling
production analytics requires those installation checks. Linux process-group RSS
sampling is a fallback, not a hard limit. macOS hard limits need a suitable
service/container policy; the crate does not claim portable hard containment.

## Offline LAN identity, pairing and request policy

Scientific IPC uses the shared preservation-safe contract codec in both
directions. Existing DuckDB requests keep their 64 KiB input and 2 MiB output
ceilings; the explicit `analysis` operation permits at most 16 MiB in each
direction for bounded immutable source DTOs. It shares the same single heavy-job
admission and process limits. Thread limits are set before Python starts,
including when package initialization imports NumPy before the worker module.
Worker execution clears the inherited environment. Only PATH, locale, timezone,
temporary-directory settings and Windows system-directory names are copied;
explicit WorkerSpec environment entries are restricted to those same names
and bounded values. Core credentials, HOME, PYTHONPATH and loader-injection
variables are excluded. NumPy/Arrow thread ceilings and Python isolation/UTF-8
settings are then supplied by the parent, before imports run.

`SecurityMode::for_installation(existing,opt_in_lan)` selects LAN TLS for a new
installation, and explicit loopback compatibility for an existing installation
until the operator opts in. `LegacyLoopback.validate_bind` rejects non-loopback
binding. LAN mode requires the route adapter to configure a real rustls TLS
listener and invoke **both** `AccessPolicy::check` and
`Credentials::authorize` for protected requests; constructing a policy alone
does not secure an Axum router. Restrict private control routes separately.

`TlsIdentity::{create,load,server_config}` keeps a per-installation P-256 identity
in owner-only private state. On Unix directories are 0700, files 0600, paths may
not contain symlinks, and linked/public/wrong-owner key files are refused. Writes
fsync and atomically rename. A local constrained CA signs a one-year server
certificate for explicit names/IPs; the CA signing key is discarded after
issuance. Existing identity is never silently replaced. Certificate renewal or
name changes require explicit re-enrollment and trust updates. The current
Web/Windows/macOS clients validate the exact HTTPS origin using the OS or browser
trust store, including an explicitly enrolled local CA. They do not persist a
separate leaf-certificate pin. `fingerprint_sha256()` (leaf DER SHA-256) is
available for independent operator verification; it is not a claimed client
pinning feature. A changed untrusted identity is rejected by TLS validation.

Browser trust comes **before sending pairing credentials**: export the local CA
certificate using a trusted local/admin channel, verify its SHA-256 independently,
and install it into the intended browser/OS trust store using that platform's
certificate controls. Visit the exact SAN hostname/IP over HTTPS. A self-signed
local installation is not publicly trusted just because TLS is enabled. Never
disable verification, accept arbitrary certificates, or send pairing tokens to
an unverified certificate-warning page. Remove the installation CA on retirement.

Only a local operator may call `PairingWindow::begin(now,ttl,attempts)`; never
expose it as an unauthenticated LAN operation. Display/QR its fresh 256-bit code
locally. A window is single-use, 1–300 seconds, at most 10 attempts globally,
rejects clock rollback, and disappears on restart. Keep only one active window
behind the core's mutex. Pairing redemption is the only LAN operation exempt
from bearer auth, and still requires TLS and trusted Host/Origin validation.
The Rust candidate implements these routes over its actual TLS listener:

* `GET /api/v1/security/status` reports the installation mode and pairing state.
* `POST /api/v1/security/pair` accepts `{pairing_code, label, client}`. Native
  clients receive the new opaque credential once over trusted TLS. A browser
  receives only an HttpOnly/Secure/SameSite=Strict session cookie. The single
  pairing window is consumed even if credential persistence fails.
* `POST /api/v1/security/browser-session` exchanges a valid native Bearer
  credential for the browser cookie used by the same-origin legacy interface.
* `POST /api/v1/security/credentials/current/revoke` (or the existing DELETE
  alias) revokes the credential, expires the cookie and closes its event stream.

Security POST bodies have a 4 KiB limit. The local `ori-core pair`
command opens the bounded window; no unauthenticated network route opens it.
The implementation and tested wire details are in `docs/rust-core.md`.

Persist only SHA-256 hashes of these high-entropy tokens; no passwords or raw
bearer tokens are written. At most 256 device credentials are admitted. Do not
put tokens in URLs, WebSocket query strings, SSE URLs or logs. Browser streaming
should use an authenticated fetch stream; native credentials belong in OS secure
storage. `AccessPolicy` requires TLS, an exact allowed Host, and exact trusted
HTTPS origins when an Origin is present; missing Origin is permitted for native
clients only together with bearer authorization. No wildcard CORS, arbitrary
forwarded Host/Origin trust, or unauthenticated LAN mutations are supplied.

## Offline releases and verification

Build with the pinned toolchain and committed lockfile; preserve the locked
legacy Python environment, isolated research interpreter/wheels, and previous
verified release directory. No startup package download or automatic upgrade is
implemented. Before cutover, the operator verifies the signed release manifest,
all payload hashes, backwards data compatibility and a private replay test.
Signing requires operator/release credentials held outside this repository;
these templates contain no production signing keys or fictional signature.
Store the prior executable for rollback, and retain a verified recovery bundle
for disaster recovery without restoring it over newer committed data during an
ordinary rollback. Publication and installation are separate operator actions.

Focused verification (synthetic only):

```sh
cargo test --manifest-path crates/ori-runtime/Cargo.toml --locked
cargo clippy --manifest-path crates/ori-runtime/Cargo.toml --all-targets --locked -- -D warnings
cargo fmt --manifest-path crates/ori-runtime/Cargo.toml --check
```

Tests exercise real owned Python fixture processes, writer/instance exclusion,
latest-data rollback, selection persistence failures, service SIGTERM versus
resume intent, cold/incomplete readiness, actual writer-lease readiness, bounded
worker output, overload, deadline/group cancellation, revocation, expired and
exhausted pairing, strict paths/permissions, host/origin policy and a trusted
local-CA TLS handshake. Hard cgroup/I/O/filesystem quotas and physical Pi/device
acceptance remain installation/hardware gates, not synthetic-test claims.
