# Replayable bounded viewport cursors

**Goal:** Replay issued History viewport cursors containing canonical original
source IDs, and reject unusable cursors before returning or caching a page.

**Frozen scope:** `ori-history` viewport validation, tests and docs only. No
Core, Archive, client, generated contract, dependency or lockfile edits. Base
`8b4d899`; one allocated Cargo job and one Rust test thread in the existing
`/workspace/.builds/ori-stream` cache, with the real installed research worker.

**Policy:** Keep the current full serialized cursor limit of 4,096 JSON bytes.
Use the Archive's nonblank exact raw-ID rule without an extra ID length or
control-character restriction. Count canonical JSON bytes, including escape
expansion, with a bounded counting writer before input admission or fresh/cache
output publication. Oversized input is Invalid (HTTP 400); oversized output is
ResourceLimit (HTTP 413). Never truncate, normalize, drop a row or invent an
alternate cursor format. Core's existing 64 KiB request-body/head policies stay
unchanged; no POST viewport route is introduced.

- [x] Write actual five-row timed continuation and oversized escape/NFD/cache
  tests first; observe the existing replay/issuance failures.
- [x] Add the bounded cursor validation and shared 4,096-byte constant; preserve
  raw source IDs, units, bits, timestamp/UTF-8 ordering and frozen boundaries.
- [x] Run focused and full History tests, strict Clippy, formatting and diff
  checks with the allocated one-job scope.
- [x] Document exact request/output behavior; release a clean scoped commit
  without push and hand the idle cache/slot back to Root.

Tests use actual Archive/History services with synthetic records. They do not
claim physical hardware, I/O-rate enforcement or wall-clock endurance.
