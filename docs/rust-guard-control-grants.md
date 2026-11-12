# Original instrument control grants

Guard grants provide cache-only authorization for Core-owned instrument actors.
They never open ports or issue RF commands. Core retains the original grant,
authentication receipt, worker instance and connection/revision gate and checks
them again before each I/O. Capturing a fresh `None` policy after queue wait is
not an execution authorization.

`GuardService::capture_instrument_control(target, control_owner)` captures an
opaque `InstrumentControlGrant`. An unclaimed receipt permanently expires when
that exact session/stream is claimed, even if it is returned later. Its weak
per-target cache is pruned and does not retain historical archive identities.
Unrelated targets remain independent.

A manual grant binds the existing token, project, target, epoch and original
lease cell. Explicit valid renewal extends the same lease; expiry, return,
reclaim, revocation or service close invalidates it. `check()` performs no SQL,
await, Core callback or credential refresh. `revoked()` wakes pending work on
ownership changes, close or expiry.

Analyzer manual acquisition retains the original confirmed stopped-worker
receipt. An explicit operator connect may bind this grant once to a newly
acknowledged Core-owned worker using `bind_manual_worker`; it never auto-opens,
steals or resumes a device. `confirm_manual_worker_stopped` supports an explicit
receiver stop before that handoff. Clones share the same once-only binding.
Core starts the explicitly connected analyzer with acquisition paused.

`confirm_instrument_coverage` checks the original grant and only confirms paused
analyzer coverage after an actual stop acknowledgement. Device acknowledgements
cannot resume Guard coverage. Explicit manual return still restores the wide
profile and requires a fresh compatible complete sweep.

Implementation and verification checklist:

- [x] Prove original negative-receipt resurrection RED, implement per-target revocation and bounded weak cells GREEN.
- [x] Prove actual manual-lease handoff, renewal/expiry, foreign tokens, queued revocation and acknowledged pause behavior.
- [x] Compare manual response fields/messages with the actual Python implementation.
- [x] Run all Guard tests, scoped strict Clippy and formatting.
- [ ] Obtain Root's independent integration review.

Verified on 2026-11-11 with Rust 1.99.1 and Python 3.12.14: all 54 Guard tests
passed with no ignored or filtered cases, including the unchanged 40 existing
tests, 13 new public-grant cases and one thousand-query weak-cache regression.
The original absence receipt resurrected after actual claim/return before its
fix; paused coverage was also incorrectly published before or without stop ACK.
Both failures were observed before their fixes. The actual Python oracle uses
owned simulated devices and checks the original response fields and error text.

```sh
ORI_TEST_PYTHON=python CARGO_BUILD_JOBS=1 RUST_TEST_THREADS=2 cargo test -p ori-guard --locked --offline
cargo fmt --all -- --check
CARGO_BUILD_JOBS=1 cargo clippy -p ori-guard --all-features --all-targets --locked --offline -- -D warnings
```

This is synthetic software authorization. Physical RF/device acceptance is not
part of these checks. Existing receiver/audio `ControlPermit` remains separate.
