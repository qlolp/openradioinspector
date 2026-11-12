# Native archive context reset implementation plan

> **For agentic workers:** Implement directly in this isolated worktree using the test-driven development cycle. Do not delegate, publish, or use compiler caches until the parent grants a Swift slot.

**Goal:** Invalidate native client evidence and late results when the same server/session archive high-water mark moves backwards.

**Architecture:** Check current health before reusing a retained live cache. A confirmed rollback advances the existing connection/request generations, invokes the existing local reset paths, and synchronously invalidates the monitoring workspace before loading a full snapshot. Nondecreasing high-water marks retain the applied replay cursor.

**Tech stack:** Swift 6.4, Foundation URLSession, SwiftUI/Combine app models, XCTest.

**Spec:** Parent's scoped native archive context reset task, based on the read-only audit of `a86adf12`.

## Constraints

- No Core guard, disarm, worker, or device-control actions; workspace choice stays client-local.
- Preserve exact source identifiers, integer frequencies, f64 levels, units, UTC, and stream selection.
- Keep same-archive replay after the last applied event, rather than jumping to the latest health watermark.
- App-model tests require hosted macOS; Linux SDK execution is separate evidence.

## Review focus

- Same identifiers after archive restoration must not retain old spectrum, history, selected event, comparison, or audio.
- A held old response must not repopulate cleared state after rollback.
- Health observed before an asynchronous snapshot must not let an older refresh overwrite a newer reset.
- Normal reconnects must replay missing committed events and retain raw values.
- Screen choice and client cancellation must not send Core lifecycle commands.

## Task 1: Controlled regressions

Files: `macos/Tests/NativeStreamingModelTests.swift`, `macos/Tests/NativeArchiveContextTests.swift`, existing comparison/audio/result/exchange model tests, and portable SDK transport tests.

- [x] Add actual URLProtocol health/snapshot/SSE rollback fixtures with unchanged IDs.
- [x] Add held comparison, audio, history/raw-sweep response regressions and a nondecreasing reconnect control.
- [x] Capture behavioral RED: hosted macOS PR 156, `bc4d304`, run 43126814229/job 130880879302, 180 tests with 107 assertions failing only in the seven intended regressions; no compile or existing-test failures.

## Task 2: Local context invalidation

Files: `RemoteDualModel.swift`, `MonitoringWorkspaceModel.swift`, subscription-only suspend in `RemoteMeasurementStream.swift`, minimal `ContentView.swift` callback and baseline-confirmation binding in `MonitoringWorkspacePane.swift`. Four existing model clear helpers also clear their old receipt text.

- [x] Read current HWM before retained-cache reuse and reject stale refresh completion.
- [x] Reset existing derived state and advance original in-flight generations on confirmed rollback.
- [x] Synchronously invalidate workspace evidence while preserving the selected project.
- [x] Load a fresh full snapshot and restart streaming at the new archive watermark.

## Task 3: Verify and document

- [x] SDK freshness checkpoint: observed policy/header RED, then focused GREEN and full Linux SDK 302 XCTest with no skips/failures plus 16 Swift Testing; seven HTTP fixtures and fourteen internal fixture children closed. Final-source repeat remains pending the parent's compiler slot.
- [ ] Run hosted macOS app-model suite and Xcode build on the exact candidate source when the parent schedules CI.
- [ ] Review the scoped diff and record exact counts, provenance, and unverified limits.
- [ ] Commit only scoped source, tests, and documentation; no push or merge.
