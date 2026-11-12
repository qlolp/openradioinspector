# Archive Session Lifecycle Receipt Plan

> **For agentic workers:** Use superpowers:executing-plans in this session. No nested agents are authorized.

**Goal:** Give Core dispatches an opaque committed-session lifecycle witness with cache-only checks, without missing Guard/Session mutations through the same Archive.

**Architecture:** Async enrollment is a command on the canonical writer, callable while the existing domain coordinator is held. A bounded weak registry owns private lifecycle signatures and per-cell atomic generations/Running/pending bits. Actual changed lifecycle transactions set only affected cells pending before COMMIT, publish a new generation after success, and restore the original generation only after proven rollback. Receipt checks never acquire that registry or consult SQL.

**Tech Stack:** Existing Rust Archive, SQLite, Tokio writer queue, atomics/weak Arc cells and existing SHA-256; no new dependency or schema.

**Spec:** Root/Core/automation frozen interface documented in `docs/archive-session-lifecycle.md`.

## Global Constraints

- Scope only `ori-archive/**`, scoped tests and docs. No Core/Runtime/Guard/Automation/shared lock changes.
- `capture_session_lifecycle(id).await -> Result<SessionLifecycleReceipt>` enrolls any existing session. Missing is NotFound; Draft enrollment check is false but holds the cell alive.
- `cached_session_lifecycle(id) -> Option<SessionLifecycleReceipt>` creates a NEW immutable receipt only for enrolled current committed Running/notpending/open.
- Original receipts have no refresh method, constructor, serialization or raw state strings. `check()->bool` is atomics only.
- The actual signature is state, created_at, started_at and ended_at plus existence/identity. Metadata/streams/raw sweeps/other sessions do not revoke.
- Every committed lifecycle transition permanently revokes old receipts, including stop/start back to equal values in one transaction. Rollback does not publish a generation change.
- Close/drop/writer exit invalidate receipts; no resurrection after reopening the same IDs.
- Only Root's allocated one-job Archive cache; no whole-workspace build, canonical rewrite, hardware or physical acceptance claim.

## Review Focus

- SQL-visible stop with delayed cache publication must never leave a fresh old-receipt check valid.
- Queue-ordered capture must observe the earlier actual transaction and must not relock the caller's coordinator.
- Failed commit/rollback must preserve old authority only if rollback is actually proven; unknown outcomes stay fail closed.
- Dropped/unused enrollments must not leave an unbounded map of ID strings or keep Archive alive.
- Same raw ID, returned signature values or new Archive instance must not resurrect an original receipt.

## Task 1: Actual Archive contract tests

- [x] Write public API tests first for Draft/Running enrollment, coordinator-held capture, no-op/raw/unrelated writes, atomic StopSession plus job cancellation, actual SQL rollback, stop/start/identity/reopen behavior.
- [x] Observe missing API RED with the allocated compiler slot; retain log outside Git.
- [x] Add deterministic private tests pausing after COMMIT, queue-ordered capture, actual failed COMMIT and 1000 unused enrollments without map growth.

## Task 2: Writer-owned receipt and commit barrier

- [x] Add `crates/ori-archive/src/session_lifecycle.rs` and minimal public reexports/methods.
- [x] Enroll through bounded writer commands; add short-lock cache-only factory and atomic-only immutable check.
- [x] Track only enrolled session mutations from actual SQL before/after fields. Ignore ordinary raw/metadata/stream writes.
- [x] Prepare pending cells before COMMIT; after success publish generation/signature without a visibility gap; after error restore only on proven rollback.
- [x] Invalidate globally only for Archive close/drop/writer exit, never ordinary unrelated session changes.
- [x] Bound live cells and key bytes; remove weak entries on final cell drop and prune dead leftovers.

## Task 3: Verification and release

- [x] Run new focused RED→GREEN tests, all existing Archive/export/backup cases, strict Clippy and formatting with one allocated job. Full Archive: 125 passed, zero failed/ignored; strict Clippy and formatting clean.
- [x] Document exact API/ordering/pending/rollback/bounds and Core/automation setup-only usage.
- [x] Keep canonical source/schema/API behavior unchanged, self-review commit-gap safety, then commit scoped branch without push.

Validation used actual Archive mutations and synthetic records. The missing API
failed compilation before implementation. Removing only the pre-COMMIT pending
marking made the deterministic, SQL-visible committed-stop regression fail;
restoring it passed. Test hooks are compiled only for private unit tests.
