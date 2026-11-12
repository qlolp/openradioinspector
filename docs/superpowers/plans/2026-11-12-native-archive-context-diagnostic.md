# Native archive context diagnostic

This draft contains only native model regressions on PR 149 source `91bf5f4`.
It is intentionally expected to fail until the implementation follows on this
same branch. It is not a release candidate or evidence of acceptance.

The tests use existing model/SDK APIs and URLProtocol-controlled responses:
same-identifier archive HWM rollback, held old metadata/comparison/history/raw
sweep/WAV completions, and nondecreasing reconnect/selection controls. No new
production seam is needed to compile these baseline regressions.

The separate SDK health cache-policy regression runs on Linux and is omitted
here so its expected failure cannot prevent the macOS model test stage.

All `macos` production source in the implementation base `a86adf12` is byte
identical to `91bf5f4`. The later generated `ORIContracts/Runtime.swift` is an
additive file; these tests and their app model dependencies do not use it.
No Core, Rust, hardware, or performance changes are included.
