# First macOS RC implementation plan

**Goal:** Deliver a personal arm64 macOS RC with a native UI, independent Rust
service and an honest two-instrument acceptance path.

**Architecture:** Reuse ori-core, ori-supervisor, the canonical archive and the
existing SwiftUI client. Platform integration owns registration and presentation;
the core remains the sole device/archive owner.

**Spec:** [Approved release specification](../specs/2026-11-14-macos-first-rc.md).

**Execution:** Inline implementation under `superpowers:executing-plans`; one
fresh whole-branch review after the sequence. Local diagnostics and the ledger
are outside Git. No automatic merge or public stable release.

## Constraints and review focus

- Preserve source data, API v1 and default shadow behavior; no automatic RF.
- RC1 has no native audio capture or external scientific runtime.
- A stale registration/readiness file must not bind the UI to another owner.
- UI exit must not stop guard; explicit stop/update must join the actual owner.
- Sleep, USB failure and disk exhaustion must remain visible coverage failures.
- Unicode paths, JSON metadata and binary64 source values retain their meaning.
- Hardware and installed-package acceptance cannot be inferred from fixtures.

## Deliverables

1. **Live core:** CoreMode.Live and a shared writable-mode predicate; explicit
   live data directory and native serial opt-in; common capability schemas and
   generated SDKs. Verify real CLI startup/write/restart/lease release, refused
   shadow/replay serial, default shadow and all three SDK fixtures.
2. **Local service:** SMAppService agent for ori-supervisor, isolated RC data,
   private status/shutdown and native reconnect UI. Verify unique ownership,
   readiness, UI exit, stale state, failure teardown and power lifecycle.
3. **Preparation:** Reuse monitoring preparation/guard models for a guided
   device/profile/range/archive/baseline flow. Verify explicit connection,
   invalidation, 60s/20-sweep baseline, 8h storage estimate and disabled RC1 effects.
4. **Package:** Build arm64 SwiftUI/ori-core/ori-supervisor; ad-hoc sign the actual
   bundle, generate manifest/checksums and DMG. Verify relocation and offline
   startup outside the checkout with no development runtime.
5. **Acceptance:** Retain diagnostics for installed-package software tests,
   the offered-input 30-minute no-audio workload, and the operator's eight-hour
   protocol. Unperformed gates stay unperformed; Pi/Windows artifacts follow Mac.

Each deliverable is a separate draft PR, with test-first regressions, appropriate
complete checks, documentation and its actual remaining acceptance limitations.
