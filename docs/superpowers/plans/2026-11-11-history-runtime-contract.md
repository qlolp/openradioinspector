# Derived History Runtime Contract Plan

**Goal:** Publish the existing Rust History HTTP operations in the additive
runtime OpenAPI and TypeScript, Swift and Rust DTOs, preserving every baseline
contract artifact byte and exact scalar/JSON values.

**Scope:** Runtime extension, minimal contract generator/rendering support,
generated runtime artifacts, shared synthetic fixtures, contract tests and docs.
No Python baseline, Archive, Core HTTP, client, dependency or lockfile edits.

**Source of truth:** `ori-core/src/history_jobs.rs`, `ori-history/src/types.rs`
and `ori-history/src/viewport.rs` at `f1673dd`.

## Contract

- History job list/submit/status/result/cancel/page/Parquet export and waterfall.
- Jobs require `derived_history_jobs`; waterfall requires
  `derived_history_viewport`. These describe route availability; unavailable
  trusted workers still return an explicit 503.
- Signed UTC microseconds use `int64`; nonnegative frequency filters/cells use
  `int64` with minimum zero. Source/viewport boundaries use `uint64`, including a
  required nullable job source boundary. No nullability-based signedness.
- Nullable typed objects use only a proper `anyOf` reference/null pair. Unknown
  metadata/metrics/enforcement keys retain exact UTF-8 and JSON values.
- Derived result rows remain open JSON objects; Parquet is a binary response.
- Original Python schemas, support map and generated baseline bytes stay fixed.

## Tasks

- [x] Add tests first for routes/gates, schema extremes/nullability and shared
  round trips across the three real codecs. Observe behavioral RED.
- [x] Add explicit integer format and narrow nullable-reference rendering while
  leaving the baseline type rules and codec bytes unchanged.
- [x] Describe the actual History wire types, errors, limits and binary response.
- [x] Regenerate runtime-only artifacts and pass common generation `--check`.
- [x] Run Python and TypeScript gates, then coordinate the existing light Rust
  and Swift caches/one-job slots for all shared codec goldens.
- [x] Verify baseline SHA equality, document semantics and release a scoped
  commit without push. Parent owns actual HTTP document binding/probe.
- [ ] Run the actual canonical HTTP document probe after Parent's owned final
  binary includes this additive contract. No Core compilation is owned here.

Synthetic fixtures exercise representable integer and float64 limits; they do
not assert that every extreme timestamp is executable within calendar/filter
semantics, or claim hardware, physical I/O or endurance acceptance.
