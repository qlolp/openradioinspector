# Background I/O Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Core bounded logical I/O callbacks covering admitted Legacy and recovery-copy work through actual completion and cleanup.

**Architecture:** Add independent public budget traits without Runtime dependencies. Pass owned optional budget Arcs into existing SQL workers; retain existing local pacing and check cancellation/deadlines around every callback. Core and History confirmed these additive signatures before implementation.

**Tech Stack:** Rust 1.99.1, rusqlite, Tokio, existing synthetic Python legacy oracle.

**Spec:** [Shared background I/O accounting hooks](../../rust-background-io-hooks.md).

## Global Constraints

- Preserve `BackupOptions`, all old entrypoints/providers and legacy JSON/goldens.
- Edit only Legacy types/sql/workspace, backup implementation, one public re-export, focused tests and docs.
- No Archive actor/lifecycle/reader, Runtime, worker, Core or shared-lock changes.
- One Cargo job, existing inactive target cache; no nested agents, push or hardware.

## Review Focus

- A denied first callback must prevent publication/science execution.
- Cancellation or expiry while a callback blocks must fail after it returns.
- Aborted async waiters must not release actual SQL/cleanup ownership.
- Copy read and write bytes must both be charged before their bounded step.
- IDs, UTC strings, binary64 bits, source hashes and Python results must survive.

### Task 1: Recovery backup callback

**Files:** `crates/ori-archive/src/{lib.rs,backup.rs,backup/limited.rs}`, `crates/ori-archive/tests/backup_io_budget.rs`.

**Interfaces:** Add `BackupIoBudget: Send + Sync` with `account(&self, u64) -> Result<()>` and `Archive::backup_with_io_budget(&Path, BackupOptions, Arc<dyn BackupIoBudget>) -> Result<Value>`.

- [x] Write/run denying, per-chunk, cancellation/deadline and aborted-waiter tests RED.
- [x] Implement pre-I/O accounting and own the Arc through reader drain/cleanup.
- [x] Run focused tests, all archive tests, formatting and strict scoped Clippy GREEN.
- [x] Commit the completed backup slice.

### Task 2: Legacy preparation and canonical writes

**Files:** `crates/ori-legacy/src/{types.rs,sql.rs,workspace.rs}`, focused legacy budget tests.

**Interfaces:** Add `LegacyIoBudget: Send + Sync` with `account(&self, u64, &LegacyCancellation) -> Result<()>`; `LegacySciencePermit::io_budget()` defaults to `None`.

- [x] Write/run tests for denied prep, sidecar/read-write chunks, post-child threshold denial/cancellation and retained ownership RED.
- [x] Thread the original permit's Arc through snapshot/threshold commands and cleanup.
- [x] Run all legacy tests with the real Python oracle plus formatting/strict Clippy GREEN.
- [x] Document verified scope and commit; request Root's independent review.
