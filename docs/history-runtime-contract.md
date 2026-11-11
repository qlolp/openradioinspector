# Derived History HTTP contract

The additive Rust runtime document describes the History service contract at
`/api/v1/history/*`; its implementation arrives in the later History and Core
delivery slices. The original Python OpenAPI and its generated baseline
artifacts are unchanged. Runtime DTOs are imported from the existing TypeScript
`runtime.ts`, Swift `ORIContracts` module and Rust `ori_contracts::runtime` module.

| Operation | Response | Required route feature |
| --- | --- | --- |
| `GET /history/jobs` | Bounded job list envelope | `derived_history_jobs` |
| `POST /history/jobs` | 202 job envelope | `derived_history_jobs` |
| `GET /history/jobs/{id}` | Job envelope | `derived_history_jobs` |
| `GET /history/jobs/{id}/result` | Derived result envelope | `derived_history_jobs` |
| `POST /history/jobs/{id}/cancel` | Cancellation envelope | `derived_history_jobs` |
| `POST /history/jobs/{id}/pages` | 202 next-page job envelope | `derived_history_jobs` |
| `GET /history/jobs/{id}/exports/{table}` | Binary Parquet | `derived_history_jobs` |
| `GET /history/waterfall` | Bounded viewport page envelope | `derived_history_viewport` |

Paths in the table have the `/api/v1` prefix. Select operations using the actual
advertised route feature and backend. The Python legacy backend does not gain
these operations. A route feature does not promise that an optional worker is
configured: an unavailable trusted worker returns HTTP 503 with
`history_worker_unavailable`. Busy admission, acquisition pressure, cancellation,
shutdown and unsafe storage also remain explicit errors. No client-supplied SQL,
executable, filesystem path or resource policy is accepted.

All three POST operations retain the actual 65,536-byte request limit and reject
query parameters. Other job GET operations also reject query parameters. Job IDs
are 32 lowercase hexadecimal characters. A cancellation body is an empty object.
A page body contains only an optional opaque `cursor` string or null, bounded by
4,096 UTF-8 bytes. It reuses the original sealed source and immutable request;
it does not select a newer live archive boundary.

The fixed export tables are `streams`, `sweeps`, `sweep_points` and
`observations`. A successful export returns `application/vnd.apache.parquet`,
`Content-Length`, `X-Content-SHA256` and `Cache-Control: no-store`. Its bytes are a
derived table, not a full archive backup or a replacement for raw records.

## Exact values and nullable models

New runtime integer fields explicitly declare `format: int64` or `uint64`.
Signed UTC microseconds use Rust `i64`, Swift `Int64` and TypeScript's existing
lossless `Integer` representation. Frequencies use signed 64-bit storage with a
nonnegative constraint through `i64::MAX`. Source and viewport boundaries use
unsigned 64-bit storage through `u64::MAX`; a required nullable job source
boundary remains `Option<u64>`, `UInt64?` or `Integer | null`.

The TypeScript runtime walker checks exact BigInt format endpoints before any
numeric schema-bound comparison, so JavaScript rounding of a JSON schema's
64-bit maximum cannot widen acceptance. Rust and Swift use native integer
decoders and generated checks for narrower field limits. The frozen baseline
codec and its legacy type rules remain unchanged.

Typed nullable object fields use a narrow JSON Schema `anyOf` reference/null
pair. The generated Rust key restoration also visits those optional nested
models. Required nullable output fields remain present as explicit null; they
cannot disappear during a round trip. Unknown metadata, result data, metrics and
enforcement keys retain their exact JSON numbers and UTF-8 names, including
distinct NFC/NFD spellings and literal serde-private names. Float64 thresholds
and displayed source levels preserve negative zero and adjacent finite values.
S-meter counts remain in their original unit.

Schemas and server validation remain authoritative for operation-specific
constraints: occupancy requires a threshold in its raw unit; other operations
accept only an absent/null threshold. Page size is 1–500. Time intervals are
half-open and ordered; frequency ranges are inclusive and ordered. Representable
int64 timestamps can still be rejected by calendar/filter validation. Codec
fixtures exercise the integer wire limits without claiming executable research
at every extreme timestamp.

## Waterfall query and raw identity

Waterfall requires session, stream, inclusive frequency bounds, columns and
rows. Columns are 1–1,024, rows are 1–100 and their product must not exceed
32,768. Optional `start_us` and `end_us` are signed integer query values; omit
null time fields. The optional `cursor` query value is percent-encoded JSON for
the structured viewport cursor, bounded by 4,096 UTF-8 bytes. It is separate from
the opaque job-page cursor and is not a `deepObject` encoding.

A viewport page is explicitly `derived: true`. It records the frozen source
boundary, quality flags and the original measurement ID, exact UTC microseconds,
raw unit, source point index, frequency and first maximum in each display bucket.
The contract does not narrow output source IDs or units to the request filters'
limits. Existing historical strings therefore remain representable. Incoming
and issued viewport cursors share a 4,096-byte limit on their complete canonical
JSON, including field names, UTF-8 and escaping. The source ID has no separate
1,024-byte or control-character restriction. An oversized input returns400;
a fresh or cached page that would issue an oversized cursor returns413 before
publication. Neither path truncates or changes the historical source identity.

The shared fixture and three codec suites use synthetic records. They verify
representation, schema composition, feature selection and original value
preservation; they do not claim hardware or physical I/O acceptance.
