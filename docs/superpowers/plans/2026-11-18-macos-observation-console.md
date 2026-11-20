# macOS observation console and RC implementation plan

Approved direction: personal macOS RC first; rebuild the operator workflow for a
new technical user around preparation → monitoring → event → original source →
AR-DV1 investigation → operator decision. Keep native SwiftUI and the shared
core as the only owner of USB and archive writes.

## Implementation

1. Start from the complete #201 → #204 → #205 → #206 → #207 → #208 stack
   (`1d9417cb6a14a7efb12cefaf4af4d4db7ef0a29e`). Preserve its fixes and receipts.
2. Replace five competing pages with Observation, Archive and labelled Settings.
   The observation console contains spectrum/waterfall (~65%) and a bounded
   event inbox with a contextual inspector (~35%), minimum window 1000×700.
3. Keep one four-stage preparation entry: project/ports/range; receive-path
   confirmation/connect; baseline (both 60 seconds and 20 compatible sweeps per
   band); storage reserve and explicit arm. Explain disabled actions.
4. Keep project/session context and separate core/acquisition/guard/archive/
   coverage states. Never call retained data LIVE after a connection failure.
   Open an event's exact original in two actions without stopping reception.
   Separate measured evidence, hypotheses and the operator's persisted verdict.
5. Move background monitoring ownership out of page lifetime; only active heavy
   views render. Scope preferences and pending work by exact context; retain
   cancellation and stale-response guards. Do not change public API or schema.
6. Verify actual controls with XCUITest; unit-test context/migration/action
   policy. Use native semantic colours, keyboard navigation and VoiceOver.
   Test light/dark, increased text and 1000×700/1200×792/1440×900 layouts.
7. Update the operator guide and release matrix with exact build evidence.

## Release gates

All 24 existing CI jobs, both Swift suites and Release Xcode must pass on the
final SHA. No deadline increases, weaker checks or retry-for-green.

The final unchanged app/core/supervisor/kit must pass: installation outside
checkout; offline/update/rollback retaining new measurements; 60-second visible
smoke (600 sweeps ×4096 and 600 observations); million-sweep ×290 history with
complete verification and warmed first 100 rows p95 ≤500 ms; two 1800-second
binary-v1/SSE loads, each 18,000 sweeps and observations, with concurrent
history/online backup and separate restore; real eight-hour two-device recovery.

Limits: UI reaction p95 ≤100 ms, queue ≤200 ms, commit→actual display ≤250 ms,
pan/zoom ≥30 fps; core ≤256 MiB, UI ≤1 GiB, background ≤1.5 GiB, RSS sample gap
≤2 s. Every offered source counts, including dropped and unpresented sources.
Preserve failed runs. Three new technical users must complete the main route
without coaching or critical mistakes and distinguish reception, guard and
paused display. Missing physical evidence remains BLOCKED/not_run.

## Execution constraints

This implementation environment is Linux without Xcode or physical devices.
Native semantic/build/UI verification uses the existing macOS CI; target-Mac
profiling, installed-package performance, usability participants and physical
acceptance cannot be substituted by Linux/synthetic checks. Audio/SDR/scientific
worker and Windows/Pi release acceptance are subsequent milestones. Publishing
or merging is not implicit in implementation.
