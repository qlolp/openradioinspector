# Archived frequency history and named bookmarks

`POST /api/v1/sessions/{session}/frequency-history` reads existing sweeps only.
Supply `stream_id`, one to four unique `frequencies_hz` (integer Hz), `start` and
`end` (UTC). Selection uses sweep start in `[start, end)`, newest first. `limit`
is 1–100; `after` is an opaque cursor bound to the session, stream, time range
and exact frequency selection. It fixes the ingestion watermark, including
late-arriving records. A refresh starts a new snapshot.

Every row retains its source sweep ID, complete acquisition interval and original
unit. A sample identifies both the requested frequency and the nearest actual
bin (ties choose the lower frequency). Requests outside the measured range
return null bin and level. There is no interpolation, unit conversion, inferred
per-bin timestamp or claim of continuous coverage. The segment digest includes
the complete grid, raw unit and recorded settings except sequence number.
Unknown settings isolate each sweep. Clients must not join across null samples,
segment changes or acquisition gaps. Matching metadata is not hardware readback.

`GET /api/v1/sessions/{session}/frequency-bookmarks` lists immutable versions,
newest first. `POST` accepts only a name and one to four `{label, frequency_hz}`
entries. Saving changed entries creates a new revision of that name; unchanged
entries are idempotent. A session holds at most 256 versions. The SQLite metadata
table is additive; sweep, candidate and detector records are untouched. Bookmarks
contain no tune, watch, generator or detector actions.

Capabilities `frequency_history` and `frequency_bookmarks` are independent.
Servers without them retain their existing receive/archive behavior. These
endpoints do not acquire measurements or schedule hardware commands.
