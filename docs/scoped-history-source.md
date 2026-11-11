# Scoped derived history source, version 1

This is a private disposable research input, not a recovery backup. It never
changes the canonical SQLite archive, historical schema, raw source values or
backup format. Worker 0.2.2 supports this input alongside existing full sealed
recovery bundles; IPC version 1 and the four Parquet table schemas are unchanged.

History reserves the shared heavy-work permit before opening the additive
`ScopedSnapshotExport`. That reader uses Archive's existing maximum two live
reader slots, one frozen read-only transaction, bounded request/deadline/record
checks and the same owned cancellation/close/JOIN barrier as raw exports.
An absent session/stream fails explicitly. Optional scope selects every session
when omitted. Time selects start instants in `[start, end)`; Hz bounds are
inclusive. A selected sweep retains its complete original arrays, so the worker
applies the existing point filter with original point indices.

The private sealed directory contains exactly the source data and its seal:

```text
records.ndjson
manifest.json
```

Each UTF-8 newline-terminated record has exactly `kind` and `value`. Kinds are
`stream`, `sweep`, `observation`, in that order, followed by exact UTF-8 ID byte
order within each kind. Values use canonical Archive JSON. Stream values
include their session ID; sweeps and observations include their original scope
and interval. IDs/JSON keys retain distinct normalization-equivalent spellings;
Hz remain integers, times retain UTC microseconds, and finite raw binary64
values retain their bits, including signed zero. No audio payload is copied.

The seal has exactly these keys (example counts/digest are abbreviated):

```json
{
  "derived_source_version": 1,
  "purpose": "derived-history",
  "records": {"file": "records.ndjson", "bytes": 1234, "sha256": "64 lowercase hex digits"},
  "event_watermark": 73,
  "filters": {"session_id": "selected-session"},
  "counts": {"streams": 2, "sweeps": 1, "observations": 1},
  "audio_payloads_copied": false
}
```

The watermark and all records come from the same transaction. The record digest
covers exact NDJSON bytes. Request filters must exactly equal sealed filters,
so replay cannot widen the frozen source. This seal has no recovery
`backup_version`, archive size or full-database checksum. Worker provenance
marks it `kind: "scoped-derived-v1"`, `derived: true`, and includes both record
and manifest digests. Cache identity includes that provenance and worker version.

The selected data **plus seal** must fit History's source quota, independent of
the size of unrelated canonical data. Records are bounded at 8 MiB for History
and the worker; Archive's original default export limit remains 16 MiB.
Admission checks each write and the final seal before publication. Data files
are synced and made read-only, the original reader is JOINED, and a same-root
rename publishes the sealed source. The parent retains it until the worker's
child and all IPC threads have JOINED and all required cleanup has completed.
Cancellation or overflow removes owned incomplete sources and never deletes
canonical records. Strong file/directory identities prevent deleting unknown
replacement paths; unconfirmed cleanup is sticky and cannot advertise a free
active heavy-work reservation.

The worker checks bounded strict JSON, kind/ID order, declared counts, exact
scope, checksums, source identities and live sidecars before publishing a cache.
It opens no live SQLite connection for derived inputs. Existing Parquet fields
and science are reused; settings/metadata JSON is compactly serialized while
preserving original decoded keys and values.

Selection payload reads/writes and worker-accounted I/O use one shared
20 MiB/s cooperative timeline when the core supplies its private shared-I/O
capability. This does not measure or enforce physical device bandwidth, native
SQLite page reads, Arrow native query I/O or hard storage/RSS limits.

Synthetic Linux tests compare derived Parquet, summary and occupancy with the
same records in a frozen full source; exercise a tiny selection from a larger
canonical archive; and check quota rejection, concurrent commits, exact raw
precision, pressure cancellation, replacement safety and restart. These tests
do not establish physical storage, instrument or Pi performance acceptance.
