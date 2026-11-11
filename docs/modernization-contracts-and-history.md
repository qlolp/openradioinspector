# Contracts, canonical precision and bounded history

The Python service retains its public routes and adds `/api/v1/openapi.json`.
`tools/contracts/generate.py` generates TypeScript, Rust and Swift models and
operation descriptors from that resource. `--check` detects drift without
rewriting files. Shared examples cover exact integer Hz, UTC microseconds,
float64 values, Unicode identifiers and extensible metadata, including real
objects whose keys resemble serde's internal number protocol. Duplicate JSON
keys and nonfinite numbers are rejected before a mutation. Integer token `-0`
retains legacy Python integer-zero semantics; explicit float `-0.0` retains its
sign. Generated codecs also distinguish required nullable fields from missing
fields.

## Original observation values

SQLite REAL can normalize a stored negative zero. A paired, versioned canonical
extension records eight little-endian float64 bytes for new non-null receiver
levels: `ori_archive_schema` version 1 and `ori_observation_level_bits`.
The source row, original bytes and event commit atomically. The existing raw
tables and main schema version 6 remain compatible. Historical rows without
bytes use exactly their stored REAL; they are neither rewritten nor guessed.
An incomplete or unsupported extension is rejected before schema writes. Reads,
NDJSON export, private recovery verification and the separate research worker
validate bytes against the REAL value. These bytes are original data and must
never be treated as an evictable analytical cache.

## History and audio

`features.sweep_summaries` advertises
`GET /api/v1/sessions/{id}/sweep-summaries`. Parameters are `stream_id`, `limit`
(1–100), opaque `after`, UTC `start`/`end` (half-open), and inclusive
`min_hz`/`max_hz`. Frequency filters select intersecting whole sweeps; peak,
frequency range and point count always describe the complete source. Results
are newest UTC start first, then exact UTF-8 ID descending; equal peaks select
the first original point. Responses contain `items` and nullable `next_cursor`,
without raw arrays. A cursor freezes its filters and ingestion watermark, so
late imports do not duplicate or change an already opened traversal.

The existing normalized-time index selects bounded IDs. Only one raw spectrum
is decoded at a time. Persistent archives permit two independent short WAL
read transactions; a full read pool returns 503 and leaves ingestion available.
No new full-archive scan or source rewrite runs during startup.

`features.audio_segment_ingest` advertises
`POST /api/v1/sessions/{id}/audio-segments` with `id`, receiver `stream_id`,
integer `frequency_hz`, requested `mode`, UTC `interval`, complete mono PCM16 WAV
`data_base64`, and lowercase `sha256`. The existing 16 MiB measurement request
budget and strict framing/deadline apply. The interval must match complete PCM
frames to one microsecond. Original WAV bytes are retained without reencoding;
the response includes `sample_rate` and `frame_count` in artifact metadata.
An ID can be committed only once (409 on conflict). Artifact and summary-only
`audio_segment.received` event commit together before publication. Existing
scoped instrument-artifact reads also serve receiver audio; control routes
retain their tinySA-specific checks.

## Offline client and service ownership

Build with Node 26 from the checked-in lock, then run
`npm --prefix clients/web run build` and `python tools/build_modern_web.py`.
The Python wheel bundles the resulting local resources at `/modern/`; `/`
continues to serve the legacy UI. The bundled application requires no CDN or
internet. A source checkout without a built modern client returns an explicit
503 at `/modern/` and keeps the legacy UI/API available.

When launched by the supervisor, the Python listener now acquires the shared
writer lease before opening its archive, authenticates private loopback runtime
requests before reading bodies, stops new mutation admission during closure,
and drains admitted work. Shutdown preserves opt-in guard intent and closes
monitoring before device workers. A cleanup failure retains ownership instead
of permitting a second writer. See `python-runtime-bridge.md` for the private
protocol. Unsupervised legacy launch remains available.

All checks in this slice use synthetic HTTP, archives and audio. This does not
establish Pi throughput, real audio wiring, USB behavior or physical acceptance.
