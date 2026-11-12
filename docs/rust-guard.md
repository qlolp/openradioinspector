# Rust monitoring guard

The Rust candidate ports the existing Python monitoring domain. Python remains
the authority for `robust-city-v1` detector behavior and monitoring-v4 records;
this work does not switch the default core or establish hardware acceptance.

The library implements projects, operator declarations, allowed signals,
confirmed baselines, legacy cadence, analysis/dispositions, preflight/storage
reports and scoped history. It also owns preparation/guard sessions, independent
receiver handoff, manual leases, narrow searches/marks, recovery, alerts and the
atomic notification outbox. Optional audio and webhook effects require explicit
Core callbacks; unavailable capabilities do not fabricate successful results.

`ori-guard` owns project intent, references, event lifecycle and device ownership.
It uses the serialized `ori-archive` actor for every write and the pure
`ori-monitoring` detector for detection before adaptive learning. It opens no
ports, database connections, audio devices, files or HTTP connections. Core
implements `InstrumentControl`; an absent capability cannot establish readiness.

The frozen constructor is `GuardService::new(Archive, Arc<dyn InstrumentControl>)`.
Core first attaches a weak policy reference, then awaits `start()`. Successful
startup means recovery attempts have finished, including interrupting old jobs
and checking any explicitly requested autostart. Spawning a task is not a ready
signal. Individual projects still require their own preflight and confirmed
baseline. Public async operations mirror the existing monitoring service;
`tick(project)` also supports deterministic replay without background scheduling.

Startup interrupts old queued/listening/decoding analyses and never replays them.
Only a saved autostart request can resume a project, after current preflight.
Analyzer and receiver health are independent: one failed worker leaves the other
registering, while archive reserve/write/retention failure stops the guard even
when both roles are manually leased. Conservative serial preparation reports
command acknowledgement separately from physical readback, which stays unverified.

Actor callbacks clone the opaque `ControlPermit` and synchronously call
`authorize_control(target, permit)` immediately before I/O, after queue wait.
Permits contain an owner token and a target-specific epoch; neither secrets nor
permits implement Debug or wire serialization. Receiver handoff does not revoke
analyzer capture. Manual expiry leaves automation paused. A receiver replacement
invalidates its manual lease, including queued commands. Tune completion rechecks
the original permit, worker instance and applied tune revision under the shared
archive coordinator. Analyzer return requires a new sweep with the original
baseline fingerprint before wide coverage resumes. Raw S-meter values retain
`s_meter_count`; no conversion or inferred decoder identity is introduced.

The archive selector uses the original `sweeps.rowid` detector cursor, not the
new ingestion sequence. Source IDs are byte-exact UTF-8. Baselines retain at
most 256 source references selected by the Python uniform-window/latest-twenty
strategy. Detection consumes at most 64 captures per pass; all evidence, quality
records and its cursor commit atomically for each sweep. Old raw rows and
baseline versions are never rewritten.

Core shutdown uses `begin_shutdown() -> Result<GuardShutdown>`. This synchronously
revokes admission and starts or joins an owned cleanup attempt. The Clone handle
exposes `intent_persisted().await` before Core cancels native polling and
`completed().await` before Archive closure. Both await the same attempt; cancelled
waiters cannot detach jobs or discard cleanup ownership. `any_armed()` is cached
and conservatively remains true while preparation or failed cleanup retains a
claim. An unsuccessful stop or archive write returns an error and retains claims;
Core must latch the failure and retain its writer lease. Explicit operator
disarm clears restart intent. `close().await` is the convenience API; a subsequent
explicit call may retry failed cleanup, while an existing handle retains its
original completion result.

The shared archive coordinator covers short read/modify/commit sections.
Hardware/audio/network effects, long calculations and waits run outside it.
Receiver investigation transitions reuse `ori-session` with a per-job effects
override carrying the original permit. The final evidence commit repeats that
same cached completion check. The Core-internal `AudioRequest::tune_reply` is
nonoptional and contains that original successful tune's worker instance,
applied revision and acknowledged UTC. Core binds its audio gate and
`not_before` to this receipt plus the original permit before opening input,
accepting PCM blocks and finalizing; it must never fetch a replacement receipt
from a later cache after an await. Missing original context fails closed. This
adds no serialized API v1 field. Exact WAV bytes and SHA-256 are stored through
Archive; event/audio reads validate project/session/stream ownership.

Notifications use Archive's atomic insert-if-absent, claim, lease and completion
CAS operations. Core receives the original stored UTF-8 body for signing and
delivery, plus private credentials; secrets never enter project/settings wire
JSON. Alerts retain acknowledgement and bounded keyset pagination. The service
does not delete raw captures to recover storage space.

Python-generated synthetic fixtures live in `contracts/fixtures/guard-*.json`.
Regenerate them with the existing core interpreter and
`crates/ori-guard/tests/generate_fixtures.py`. Tests use temporary actual SQLite
archives and callback fakes; no physical RF/readback/audio validation is implied.
The focused tests compare actual Python records, evidence, hysteresis, gap
handling and cursors. Separate assertions inspect original binary64 bits,
microsecond UTC, byte-exact IDs/Unicode keys, reserved metadata object keys and
integers beyond u64. A conservative serial context fixture checks declarations
and inspected LNA/RF context without opening a port or claiming readback.

Verified 2026-11-11: all 40 focused tests pass with real temporary archives and
fake effects, including startup/opt-in rejection, partial-worker independence,
archive failure, stale permits/reconnect, stop-versus-tune, manual expiry/search,
WAV/hash preservation, shared shutdown barriers and cancelled cleanup waiters.
Strict all-target/all-feature clippy (`-D warnings`) and formatting pass. Run
`cargo test -p ori-guard --all-features --offline -j 2` after dependencies are
prepared. Build artifacts stay outside the checkout; the integrating workspace
owns the root Cargo lockfile. These checks do not establish hardware acceptance
or complete semantic validation of legacy scientific extension payloads.

## Domain lookup and validation contract

Guard errors retain the Python monitoring resource's lookup/validation order;
Core maps `http_status()`, `api_code()` and `to_string()` without guessing from
HTTP paths. `GuardError::RecordNotFound { kind, id }` retains the exact requested
UTF-8 identifier for internal callers. Its public message, and the existing unit
`NotFound` message, are `'Запись наблюдения не найдена'`, including the quotes
produced by Python `str(KeyError(...))`. The authority does not include a project
ID in that message; one must not be invented or normalized. Fixed scoped lookup
messages use `NotFoundContext(&'static str)` with the same KeyError quoting.

The baseline resource looks up its project before checking the confirmation
payload, as the Python API wrapper does. This deliberately includes the public
wrapper semantics: direct Python `create_baseline` alone checks confirmation
first. After confirmation, armed-project refusal precedes name/location checks.
Allowlist creation looks up the project, rejects unknown fields, checks the
1,000-rule catalog, then validates the range, watch flag and name. Review looks
up project and event and checks event ownership before validating the verdict.

Event-source and audio reads follow their different Python order: they resolve
the event and its project ownership without requiring a project lookup. Public
event/baseline detail reads check project scope first. Missing audio, foreign
events, foreign records, absent raw sweeps and absent/scoped alerts retain their
distinct authoritative messages.

Manual and search requests validate their required field set first. They check
the active manual lease before frequency/span/label values, so an inactive guard
does not turn into an unrelated numeric-validation error. Token bytes, lease
expiry, worker replacement checks, control epochs, original tune receipts and
the final execution-time permit checks remain authoritative. Input validation
does not start, stop or tune devices.

Status captures the project and in-memory leases under the existing domain
coordinator, then releases it before worker callbacks. A completed analyzer
return cannot combine newly restored wide coverage with an older manual lease.

`crates/ori-guard/tests/generate_validation_fixtures.py --check` exercises the
actual Python monitoring API against an owned SQLite archive, without a listener
or physical devices. Its 89 cases include composed/decomposed Unicode IDs, missing
and foreign resources, empty/malformed field sets, confirmation/state precedence,
catalog capacity, lease-before-value errors and a successful metadata-preserving
review. Failed fixture requests compare all SQLite dump rows and the connection's
write count before/after; worker start/stop/tune calls fail the oracle. Rust uses
the same records and asserts unchanged SQLite database/WAL bytes, raw sweeps,
archive events, projects and catalogs on errors, plus original f64 bits and
unknown metadata on review.

The fixture pins lookup precedence, validation messages and scoped lookup
errors for these requests. Backend readiness and physical diagnostics require
separate verification.
