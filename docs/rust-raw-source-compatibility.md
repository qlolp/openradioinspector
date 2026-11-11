# Raw source grids and integer frequencies

Archive storage follows the existing Python measurement contract and SQLite's
nonnegative signed 64-bit frequency range. It does not use JavaScript's safe
integer range as a scientific or archival limit. Frequencies beyond binary64
display precision stay exact integers; each client separately decides whether
its display supports them. No hardware profile or supported RF range changes.

The previous Rust candidate also imposed a universal 16,384-point limit absent
from Python's `Sweep`. That restriction is removed. Arrays remain nonempty,
equal length, ordered in frequency and finite in their original level unit.
Existing byte, writer admission, response and HTTP budgets still reject a large
record explicitly and atomically. No array is truncated to fit a display or
stream budget. Protobuf and per-device limits remain separate, advertised rules.

Regressions first reproduced actual Python acceptance followed by Rust
rejection for a 20,000-point sweep and receiver/candidate frequency
9,007,199,254,740,993 Hz. They verify complete raw arrays, negative-zero/ULP
bits, UTC microseconds, commit/reopen and unchanged state on byte-budget
rejection. The existing query-bound regression now checks exact accepted
integers alongside genuine negative/SQLite-overflow rejection. Synthetic
success does not establish hardware operation at those frequencies.
