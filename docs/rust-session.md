# Rust session domain

`ori-session` implements the candidate, investigation, named-reference,
supplied-result and signal-list business surface against the Python SQLite
`SessionService` authority. Golden fixture generation runs the actual Python
service against temporary synthetic archives. Scientific calculations remain
Core research-pool callbacks; no scientific algorithm is duplicated here.
Synthetic Archive/effect tests pass. Device, full Core HTTP and physical
acceptance are separate integration gates, not claims of this crate.

## Integration contract

`SessionService::new(Archive, Arc<dyn SessionEffects>) -> Arc<Self>` is shared by
Core and Guard. Methods are async and return `SessionResult<Value>`. Errors expose
`http_status`, `api_code`, and the authority's public message. Core owns HTTP
envelopes and session/device start/stop; this domain owns candidate/investigation
transitions, named references, supplied measurement results, and signal lists.

`create_sweep_reference` internally returns `{reference, created}` so Core can
return the reference DTO with HTTP 201 or replay HTTP 200. The public wire shape
does not gain that internal wrapper. The reference limit is **100**, exactly as
the Python authority, and comparison compatibility remains a Core research-pool
callback.

`SessionEffects::tune(TuneRequest) -> SessionFuture<TuneReceipt>` receives owned
session/stream IDs, integer-Hz frequency and optional original mode.
`validate_reference(session, sweep) -> SessionFuture<()>` delegates the compare-
self compatibility gate to Core's shared research pool. Neither callback may
write canonical data on this domain's behalf.

`TuneReceipt` is opaque and Clone, without Debug or Serialize. It contains an
`Arc<dyn TuneCompletionCheck>` whose synchronous `revalidate()` verifies the
original worker/applied-tune generation and original authorization epoch without
I/O. Guard uses `start_investigation_with_effects` or
`start_next_investigation_with_effects` with a per-job adapter capturing the
original permit. It never obtains a replacement permit for delayed work.

`with_sources` injects a UTC clock and ID factory for deterministic fixtures.
Production constructors use the system UTC clock and UUIDs.

The async method groups are:

| Business surface | Methods |
| --- | --- |
| Candidate | `create_candidate`, `get_candidate`, `list_candidates`, `dismiss_candidate` |
| Investigation | `enqueue_investigation`, `get_investigation`, `list_investigations`, `next_investigation`, `start_investigation`, `start_next_investigation`, `matching_observation_ids`, `complete_investigation`, `fail_investigation`, `cancel_investigation` |
| Reference | `list_sweep_references`, `create_sweep_reference` |
| Supplied result | `measurement_catalog`, `save_measurement_result`, `list_measurement_results` |
| Signal list | `signal_list_export`, `signal_list_import` |

IDs and route/session arguments are `&str`; creation bodies are `Value`.
Optional state/methodology/stream filters are `Option<&str>`, evidence/result
limits are `Option<usize>`, and all methods return `SessionResult<Value>`.
`get_session` is a read helper; Core remains responsible for generic session
creation/start/stop. Full public signatures are exported from the crate.

Guard's internal `complete_investigation_with_check` accepts the original
cache-only completion check. It reloads current state and checks authorization
inside the same coordinator critical section as evidence selection and commit;
checking only before calling the service leaves a revocation race. Public
`complete_investigation` retains its existing API.

Trusted manual bindings can supply one original cache-only authority check with
`start_investigation_with_effects_and_check(session, id, effects, check)` or
`start_next_investigation_with_effects_and_check(session, id, effects, check)`.
Both accept `Arc<dyn TuneCompletionCheck>`. The same object is checked inside the
Archive domain coordinator immediately before `tuning` is saved, after preceding
reads, and is composed with the returned tune receipt immediately before
`listening` is saved. A revoke while waiting for the first lock leaves the queued
job, candidate and events unchanged and executes no callback. A revoke after a
callback enters the existing failed-job/candidate-queued terminal path; it does
not undo an acknowledged or partially executed physical tune.

This original check is separate from automatic eligibility and priority rules.
Manual direct start still targets its requested queued job; confirmed-next
still requires the actual next job. Existing constructors and unchecked public
methods keep their original behavior. Checks must use their original cached
epoch and perform no I/O or permission renewal. Core owns HTTP credential
capture, device execution gates, accepted-operation lifetime and actual JOIN;
these library methods do not create an operation supervisor or interpret an
HTTP disconnect as credential revocation. A caller-owned start task retains its
original check even if its result observer disappears.

## Transactions and evidence

All instances use the same `Archive::domain_coordinator()` Arc, including Core's
stop path. Hold it only for read/modify/commit. Release it before tune or research
callbacks; reacquire it and reload current session/job state before applying the
result. Cancellation or stop wins over a late successful or failed effect.

An investigation commits `tuning` before the callback. A completed callback is
not enough to enter `listening`: the job must still be the same active work and
its receipt must remain valid. Completion requires actual receiver observations
matching session, target stream, integer frequency, normalized requested mode
(or any mode for None), and observation start at or after investigation start.
An overlapping observation beginning earlier is not completion evidence.

Validation follows the individual authority models. Candidate peak/score fields
use Python's numeric coercion rules, whereas supplied result fields require
actual finite JSON numbers and reject booleans. These policies are tested
separately, including their ordering before a missing-session error.

Typed bounded SQL selectors live exclusively in Archive's independent
`session_queries` module. No SQL, hardware access, or growing history registry
belongs in `ori-session`. Queries use one Archive read snapshot and its response
and time budgets; oversized evidence must fail rather than silently truncate.
Candidate and investigation lists return all matching rows or a query-budget
error. Result limits select the latest N rows and return them in chronological
order, rather than returning the oldest page. Ordering uses canonical UTC and
UTF-8 ID keys, including archives stored with SQLite UTF-16 encoding.

Signal-list raw JSON is limited to 262144 UTF-8 bytes and 256 entries. The
domain receives an already parsed outer `Value`; the HTTP owner must retain
the separate 524288-byte import-request limit, including escaped JSON overhead.
Nested duplicate fields, non-finite constants and non-finite numbers retain the
Python authority's distinct errors. No URL or file fetch is performed.

## Verification

The fixture generator is `contracts/fixtures/session-generate.py`; its output
pins authority source hashes, generator hash, Python/SQLite and Unicode versions.
The main fixture exercises 161 real Python SQLite actions. Eleven Session tests
cover that fixture, exact DTO/event/error comparisons, all nine result
methodologies, reference replay and the actual 100-reference limit, foreign
ownership, matching evidence, failed/blocked effects, cancel/stop races and
original-permit revalidation at completion. Four dedicated Archive selector
tests cover ordering, UTF-8/UTF-16 ties, exact evidence and byte-budget failure.

Reproduce with the repository on `PYTHONPATH` and an existing core Python
environment; the generator writes only its owned fixture and temporary archives:

```sh
PYTHONPATH=. /path/to/core-python contracts/fixtures/session-generate.py
CARGO_BUILD_JOBS=2 cargo +1.99.1 test -p ori-archive --test session_queries -- --test-threads=2
CARGO_BUILD_JOBS=2 cargo +1.99.1 test -p ori-session -- --test-threads=2
CARGO_BUILD_JOBS=2 cargo +1.99.1 clippy -p ori-session --all-targets -- -D warnings
CARGO_BUILD_JOBS=2 cargo +1.99.1 clippy -p ori-archive --test session_queries -- -D warnings
```

Dependencies are the verified Archive coordinator/query changes `36d1f778`,
durable-close fix `53172c8`, Python integer-zero codec fix `bb84d50`, canonical
candidate precision fix `876cf8d`, and its stop-path query correction `3f83aae`.
The latter correction was found by the actual stop-versus-tune race test.
The integration owner regenerates the workspace lock after adding this crate;
Unicode normalization/category dependencies are pinned to Unicode 15.0,
matching the fixture's Python runtime.

Candidate precision is canonical, without a cache workaround or historical
guessing. New peak/score values retain their f64 bits; historical absent bits
fall back to exactly the stored REAL. The original pre-fix golden remains
byte-for-byte at SHA256 `026112926cdffb7bc1776510ddf05dd670856ebaf09eee4253c0750bb92db689`.
Explicit regeneration after the independent precision fix produced SHA256
`0b7514b01422381fa45c7d72b8e257b6c2471afe3b44bfef43ed0e0eac533fba`.
`session-domain-precision-diff-v1.json` records the eight affected case values,
including negative-zero bits and exported signal-list input, separately from
source/runtime metadata. No golden value was normalized to hide the bug.
