# MacBook Air acceptance: load and eight-hour live run

Spec: user-approved conversation plan of 16 November 2026. Implements only points 2 and 4; fixes of demonstrated blockers are included. Base: PR201,7284657. Operator runs the prepared kit on their Mac with tinySA Ultra and AR-DV1. This Linux build environment cannot produce physical acceptance.

## Deliverables

1. A signed arm64 personal RC plus a separate engineering kit and StartTests.command, with its own runtime; RC1 itself still excludes Python, research and audio.
2. One final source revision and actual executable hashes bind both reports. No combining passed results from different runtime builds.
3. Full load:1800 real seconds,18000 proposed4096-point sweeps and18000 receiver observations, native UI, bounded history, online owner backup and separate restore; all original IDs/intHz/UTC/units/f64 bits preserved.
4. Native p95 reaction<=100ms, queued command acknowledgement<=200ms, commit-to-presented<=250ms, pan/zoom>=30fps. Missing evidence fails closed. GPU completion is not presentation.
5. Eight real hours with confirmed profiles, baseline>=60s AND20 complete sweeps, guard/events, independent AR-DV1, source search/return, Command-Q/reopen, USB recovery, sleep/wake, original history and restore.
6. Explicit private control/telemetry only; no administrative HTTP API, automatic RF, firmware flash, archive rewrite or deletion.

## Tasks

- [x] Strict bounded evidence/identity/native and hardware verifier, positive and negative fixtures.
- [x] Actual packaged ori-core post-commit observer and opt-in private backup control, common Mac monotonic clock. Linux CLI/commit/backup/restore and real duplex tests pass; Mac clock requires native execution.
- [ ] Native source/interaction trace and actual presentation callbacks. Primary Canvas needs a coded same-pass marker plus cropped ScreenCaptureKit observation; Metal retains presented callbacks. No image capture is saved.
- [ ] Controller for load and live observation, operator prompts with measured state/source proof, failure diagnostics and explicit separate test archives/service ownership.
- [ ] Self-contained engineering packaging, CI smoke/artifacts, operator documentation; full available suites, one whole-branch review, regressions for Important findings, PR.

## Software evidence and remaining host checks

The implementation includes all five tasks. Linux verification:840 Rust tests without ignored/filtered cases, full legacy release runner2385 tests,38 focused portable acceptance regressions, generated contracts, TypeScript/Web/research suites and real-Core client interoperability. Full Rust compilation/doctests and actual executable runs were separated to reclaim only task-owned compiler caches under the constrained build disk; exact test executable hashes were checked before execution. No test thresholds or workload denominator were weakened.

The single read-only whole-branch review found five Important issues: missing isolated owner entry point, first-poll readiness race, frozen-source false acceptance, view cancellation of native input and repeat service registration. Portable regressions demonstrate RED→GREEN for the first three and cleanup protocol; native lifecycle/ownership regressions are supplied for Mac CI. Controlled USB errors preserve the later wire window; unexpected failures freeze it. The native driver is process-owned and attaches only to a real workload source. Cleanup requires both private core stop ACK and dedicated SMAppService removal.

Mac compilation, relocated CPython/CLI smoke and Swift lifecycle tests must pass on the final PR SHA. The full native30-minute and physical8-hour tests remain **not_run** until the operator runs the kit on the target Mac. The short UI/USB/sleep minimum intervals are operator protocol steps; the verifier confirms transitions, source freshness and the full continuous8-hour window. Full installation/update and usability acceptance remain deferred.

## Acceptance and failure behavior

- Controller discovers and verifies the actual supplied .app/manifest. Source revision, core/UI hashes, host, firmware and settings are recorded before work.
- New private output directories only; actual free space budget includes active+backup+restore. Read-only source observations and bounded buffers; no automatic source deletion.
- Hardware actions remain physical/operator actions. Checkboxes never substitute for archive/worker/source transition evidence.
- Scan timeout or other spontaneous loss marks a failed run even after recovery. Preserve a bounded wire ring and all original sources; no blind restart or timeout relaxation.
- A changed runtime/native/serial path needs new final-build proof. Short CI smoke cannot close either real gate. Build-only success leaves hardware/native/full-load not_run.
- Full installation/upgrades/usability studies, public release, audio, scientific worker, new radio features and firmware promotion are deferred.
