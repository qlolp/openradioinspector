# Native archive context diagnostic

This draft began with only native model regressions on PR 149 source `91bf5f4`.
The test-only `bc4d304` baseline was intentionally red: hosted macOS run
43126814229/job 130880879302 executed 180 tests with 107 failed assertions in
only the seven intended behavioral regressions, without compile errors or
existing-test failures. Later commits on this same branch contain the candidate
implementation. It still requires verification on its exact hosted macOS head;
the historical baseline and a source review are not evidence of acceptance.

The tests use existing model/SDK APIs and URLProtocol-controlled responses:
same-identifier archive HWM rollback, held old metadata/comparison/history/raw
sweep/WAV completions, and nondecreasing reconnect/selection controls. No new
production seam is needed to compile these baseline regressions.

The SDK health cache-policy regression was omitted from the initial baseline so
its expected failure could not prevent the macOS model test stage. It is now
included with the fix. Its separate Linux checkpoint observed the expected four
policy/header assertions before the fix, then 302 XCTest cases with no skips or
failures and 16 Swift Testing cases, with all owned synthetic fixtures closed.
The new app-model implementation is not validated by that portable checkpoint.

All `macos` production source in the implementation base `a86adf12` is byte
identical to `91bf5f4`. The later generated `ORIContracts/Runtime.swift` is an
additive file; these tests and their app model dependencies do not use it.
No Core, Rust, hardware, or performance changes are included.
