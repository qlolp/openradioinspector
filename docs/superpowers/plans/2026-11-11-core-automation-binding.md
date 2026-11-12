# Core Automation Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. The parent assigned native execution without nested agents.

**Goal:** Bind the released automation policies to original Core authority and owned effects while preserving the Python HTTP behavior.

**Architecture:** Original authentication and Guard grants belong to the explicit policy epoch. A separate Running setup phase captures immutable Archive and worker receipts before effects; the shared SessionService owns canonical evidence/state changes and the shared research pool owns scientific children.

**Tech Stack:** Rust 1.99.1, Tokio, ori-automation/Archive/Session/Guard/Runtime, existing Python detection RPC, owned loopback HTTP and SQLite fixtures.

**Spec:** [Binding design](../../core-automation-binding.md), [modernization plan](../../plans/2026-11-11-modernization.md), and [released automation domain](../../rust-automation.md).

## Global Constraints

- Scope: new `crates/ori-core/src/automation_binding/**`, new tests/docs; the parent owns Core dependency/field/HTTP/shutdown glue.
- Separately authorized library edits: `ori-automation/src/effects.rs`, `service.rs`, and focused setup tests only.
- No compiler starts until the parent releases a slot; never use an active target cache.
- Keep runtime policies default-off and Draft opt-in valid; no automatic hardware opens, grant recapture, or inline scientific algorithms.
- Preserve source IDs, integer Hz, UTC intervals, original units, exact f64 values, and serialized contracts.
- Keep aggregate parity/hardware claims false until their actual gates pass.

## Review Focus

- A Draft policy must remain harmless and become usable after explicit start; test actual Python and Core HTTP.
- Stop after the Running read but before setup must prevent effects; test an actual committed stop with a controlled setup barrier.
- Revoking or replacing one original policy must not refresh an old task or invalidate an unrelated policy/session; test both sides.
- Cancelling an HTTP waiter or scientific request must retain ownership through real drain/reap; test canonical output remains absent and close waits.
- A successful own tune must remain eligible for post-ACK evidence, while replacement/revocation must fail; test the retained actual worker and TuneOutcome.

---

### Task 1: Preserve Draft semantics while separating Running setup

**Files:** `crates/ori-automation/src/effects.rs`, `service.rs`, new `tests/running_setup.rs`.

**Interfaces:** Add default `AutomationEffects::setup_running(AutomationScope, Arc<dyn TuneCompletionCheck>) -> SessionResult<Arc<dyn TuneCompletionCheck>>`. Existing providers keep the original check. Call it after the actual Session Running guard inside the owned task; compose old and new checks. Add `AutomationService::configure_with_check(kind, session, payload, original)` without changing existing configure calls; revalidate after queued coordinator/boundary waits before mutation.

- [x] Write the Draft→Running and stop-during-setup tests using actual Archive commits and a private setup barrier; assert zero effects after stop and independent-session progress. Include queued revoked opt-in with no policy/event mutation and a provider attempting to replace original authority.
- [x] Add only the additive trait declaration, run the focused test, and observe RED because setup is not called.
- [x] Wire setup after the Running guard and retain both checks.
- [x] Run the focused tests, all ori-automation tests, and its existing Python authority fixtures; commit this library change separately (`42af387`, plus canonical ACK handoff `dd4c6a1`).

### Task 2: Bind HTTP policy authority and lifecycle

**Files:** new `automation_binding/{mod,http,authority}.rs`, new `tests/automation_binding_candidate.rs`, `automation_http_oracle.py`, and its generated fixture.

**Interfaces:** `async initialize(&Arc<Core>) -> crate::Result<()>`; `handles(&Method,&[String])->bool`; `get/post` accept `&Core` and the original AuthReceipt. Set `Core.automation: OnceLock<Arc<AutomationBinding>>`. The parent owns thin integration glue. Archive provides enrollment and cached Running receipts; devices provides opaque RetainedWorkerHandle.

- [x] Record actual Python HTTP defaults/errors and Draft→start detection with no worker opened.
- [x] Write public Core HTTP regressions against those values; leave Rust execution pending the compiler slot.
- [x] Observe behavioral RED against the existing missing routes (six actual `501 candidate_route_unavailable` failures).
- [x] Implement default-off initialization, serialized policy replacement, original receipt storage, independent bounded scopes, and retained shutdown.
- [x] Cover permanent Guard absence/manual revocation, lifecycle replacement, invalid/manual foreign authority, and independent-session cases; preserve the released actual Guard claim→return regressions. Observe the missing-token disable exception RED before removing it.
- [x] Run eight public Core HTTP tests and verify original error messages, no policy secrets in results/events, and no implicit hardware start.

### Task 3: Connect actual scientific and device effects

**Files:** new `automation_binding/{effects,science}.rs` and focused Core tests/owned child fixtures.

**Interfaces:** Detection RPC uses `operation=analysis`, `method=automatic_detection_regions`, the owned Sweep, finite threshold and exact bounded maximum. Shared Runtime helper and ResearchPool retain the real child until reap. RetainedWorkerHandle gives the original execution gate; manager tune returns TuneOutcome.

- [x] Add real child and owned worker tests for original detection results, cancellation/reap, Guard/auth revocation, and successful own retunes.
- [x] Implement closed RPC validation and same-worker tuning after actual public-route RED, without opening or refreshing anything.
- [x] Verify candidate/investigation writes use the same SessionService and complete canonical commits before public success.
- [x] Verify all 125 Core tests across the all-feature run and corrected startup/tail targets, all 17 Automation tests, strict all-target/all-feature Clippy, formatting, and doc tests. No hardware or macOS/Windows runtime execution was claimed. Windows cases assert existing fail-closed platform boundaries without skips.

The pre-existing startup cancellation test's own competing lease probe failed
in the full run. The parent authorized a separate test-only correction: poll
one boxed Core startup future to its first await, verify its already-captured
external writer lease, then drop that same future and retain the actual SQL
cleanup probe. This changed no production behavior or deadline.
