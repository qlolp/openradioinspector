# Candidate scalar precision

SQLite REAL can normalize a negative zero. Candidate creation and in-memory
responses previously retained the sign while a list or reopened archive lost
it. New candidate writes now preserve the original binary64 representation of
`peak_level` and `score` alongside the unchanged original columns. Their values,
the candidate and its event commit in the same writer transaction.

The independent version-1 pair is `ori_candidate_precision_schema` and
`ori_candidate_scalar_bits`. Each non-null scalar has eight little-endian bytes;
null updates remove its supplement. Both-null candidates need no supplementary
row. Existing rows without supplements use exactly the stored REAL value;
startup neither guesses signs from old events nor backfills history. Main
archive version 6 and the observation precision extension remain unchanged.

Reads validate finite bytes against the stored numeric value. Partial or future
extension metadata is refused before writes. Recovery backups verify these
canonical bytes before publishing a success manifest. They are original values,
not an evictable display or analytical cache. This delivery slice verifies Python
get/list/reopen/restore results. The matching Rust table layout and scientific
snapshot preservation arrive in the later Archive delivery slice.
