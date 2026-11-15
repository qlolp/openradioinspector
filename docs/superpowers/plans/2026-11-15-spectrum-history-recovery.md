# Spectrum history and receive recovery

Approved scope: the conversation plan of 15 November 2026, following the review
of tinySA PCSW, QtTinySA and PR #191. Preserve the existing SwiftUI workbench,
raw measurements, UTC intervals, user edits and per-device ownership. No merge
of existing PRs, no new scheduler, no automatic transmitter activation.

## Task 1: Correct the evidence inventory

Correct PR #191's current document in a separate branch based on its head.
Distinguish UI availability, configured options and physically verified behavior.
Fix audio/IQ/calibration claims, threshold semantics, image captions, paths and
unsupported time/device/version inferences. Preserve screenshots and observations.
Verification: review the complete documentation diff and links; no prose tests.

## Task 2: Diagnose transactions and recover receive safely

Add bounded transport metadata diagnostics without raw spectrum/port contents.
Retain diagnostics across failure, expose them through the instrument API and
provide explicit recovery through worker stop/new connection/start. Never send a
new command into an incomplete exchange or mark reception fresh before commit.
Write failing tests for partial/late frames, recovery and device independence.
Expected: relevant synthetic transport/worker/API tests pass; no hardware claim.

## Task 3: Add archived frequency history and persistent bookmarks

Core-owned read-only paginated frequency history: 1-4 requested integer-Hz
frequencies, actual bins, original units, sweep UTC intervals/source IDs, gaps and
configuration segments. Persistent versioned bookmark sets contain labels and
frequencies, separate from observations, detector rules and device commands.
Use existing archive and cursor conventions; add API v1 and contract coverage.
Write failing tests for pagination, incompatible grids/settings, gaps, scopes,
invalid inputs, persistence and no acquisition side effects.
Expected: targeted core/API tests and existing relevant archive tests pass.

## Task 4: Integrate macOS history, bookmarks and reference comparison

Typed Swift SDK models validate source and requests. Add a workbench history pane,
four frequency selections, saved sets and jump to original sweep; actions from
marker/candidate. Reuse existing core comparison. Capability-gate unsupported
servers. Bound caches and rendering; leave collection owned by the core.
Write failing model/SDK tests before implementation. Expected: Swift suites and
Xcode build pass; inspect small-window UI if available.

## Task 5: Verify and publish reviewable changes

Run full available release checks, Swift suites and build. Test hardware only
through its sole owning core, preserving other active sessions. One and two bands
must pass 30 minutes after learning before an 8-hour dual-device endurance test.
Compare scanraw separately; do not change the default transport without evidence.
Run one fresh whole-branch review, fix important findings, document exact results,
commit and publish draft PRs without merging any existing PR.

## Review Focus

Untrusted cursors and JSON bounds, archive provenance, unit/configuration changes,
late serial prompts mistaken for ACK, recovery ownership, bookmark persistence,
cross-session responses, UI cancellation/stale results and bounded memory.
