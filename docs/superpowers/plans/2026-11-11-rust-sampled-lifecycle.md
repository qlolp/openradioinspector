# Rust Sampled Lifecycle Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans in this session; no nested agents are authorized.

**Goal:** Verify exact existing lifecycle sources and public event state through two owned Rust candidate restarts.

**Architecture:** Reuse the benchmark lifecycle generator/ledger/verifiers unchanged. Compose the original release process owner with a minimal stdin-lifeline CLI adapter, a new orchestration module and a disk-backed event proof.

**Tech Stack:** Standard-library Python, existing benchmark modules, real Rust candidate HTTP/CLI.

**Spec:** `docs/superpowers/specs/2026-11-11-rust-sampled-lifecycle-design.md`

## Global Constraints

- Default604800 source seconds, hourly168 sources of each kind,1680 seconds of closed PCM; label accelerated seven-day sampled timeline.
- Candidate is explicit, immutable by SHA; absent binary/routes => unavailable.
- Fresh replay archive and output only; never overwrite/delete previous or unknown data.
- Original owned process-tree JOIN must complete before restart. No Rust compilation, Core/Archive/Guard changes, nested agents or hardware commands.
- Real public source/event proofs, exact integers/UTC/f64/IDs; no fake Core API success or silently reduced denominator.
- Public tail bounds do not claim internal memory or physical endurance acceptance.

## Review Focus

- Missing/incompatible candidate must leave explicit unavailable evidence and create no canonical source.
- Duplicate/noncontiguous/mutated event payloads must fail rather than normalize.
- Changed binary or uncertain child cleanup must fail, retain evidence and prevent restart.
- Reverification must reset only disposable verifier flags, never offered inputs or canonical data.
- Short smoke runs cannot claim the full seven-day sampled workload.

### Task 1: Explicit outcome and exact event proof

**Files:** new `benchmarks/ori_bench/endurance.py`, `benchmarks/ori_bench/endurance_proof.py`; new `benchmarks/tests/test_endurance.py`.

- [x] Write missing-candidate/output-ownership and event sequence/float/Unicode mutation tests; observe missing-API RED.
- [x] Implement `run_endurance(output,candidate=None,samples=168,run_id=...)` and bounded disk-backed event checkpoint helpers.
- [x] Run focused tests; keep unavailable distinct from successful execution.

### Task 2: Owned candidate lifecycle and real verification

**Files:** new `benchmarks/ori_bench/endurance_node.py`; complete orchestration and tests.

- [x] Use original `OwnedCommand` plus trusted CLI adapter; test startup/stop failures with non-HTTP command fixtures only.
- [x] Run original lifecycle producer on actual retained candidate, create unarmed Guard project, start session metadata, freeze events.
- [x] JOIN/restart same archive twice; reuse original raw/WAV/history verification and compare exact event prefix/watermark.
- [x] Assert bounded public tails and no active workers; retain actual preflight result without hardware readiness claims.

### Task 3: Full sampled timeline and documentation

**Files:** new `docs/rust-sampled-lifecycle.md`, additive pointer in `docs/modernization-benchmarks.md`.

- [x] Run all existing benchmark tests plus new regressions.
- [x] Run real default168-sample candidate scenario and record exact counts/PCM/event/restart proof outside Git.
- [x] Document explicit candidate environment, limitations and failure ownership; verify source/static diff and commit scoped work without push.

## Recorded verification

The new17 regressions pass without skips. Existing benchmark discovery ran47 cases at its earlier15-new-case revision, with2 unchanged opt-in browser tests not enabled; no new skip was introduced. Source AST and diff checks pass. Missing API RED, actual stop-publication and idle-inventory corrections, original86400x overload, and post-load Guardpreflight timeout are retained outside Git. A real full168/4096-point run at explicit8640x verified all sources/WAVs and506 events through2restarts/all3ownedJOINs. The independent proof passes; overallunavailable remains truthful because NDJSON is not advertised. Post-loadpreflight responsiveness and all hardware/Pi/continuousendurance gates remain unrun or unresolved.
