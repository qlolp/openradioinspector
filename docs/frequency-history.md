# Archived frequency history and named bookmarks

`POST /api/v1/sessions/{session}/frequency-history` reads existing sweeps only.
Supply `stream_id`, one to four unique `frequencies_hz` (integer Hz), `start` and
`end` (UTC). Selection uses sweep start in `[start, end)`, newest first. `limit`
is 1–100; `after` is an opaque cursor bound to the session, stream, time range
and exact frequency selection. Timestamp validation and ordering retain microseconds;
rendering alone uses display dates. It fixes the ingestion watermark, including
late-arriving records. A refresh starts a new snapshot.

Every row retains its source sweep ID, complete acquisition interval and original
unit. A sample identifies both the requested frequency and the nearest actual
bin (ties choose the lower frequency). Requests outside the measured range
return null bin and level. There is no interpolation, unit conversion, inferred
per-bin timestamp or claim of continuous coverage. The segment digest includes
the complete grid, raw unit and recorded acquisition settings. The top-level
sequence counter and derived `measurement_qualification` annotation are omitted
from the digest. Both remain available on the original sweep. A sweep with no
recorded acquisition settings forms its own segment. Clients must not join across null samples,
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

## macOS workbench

Open **История уровней**, enter up to four frequencies in MHz and choose the UTC
window. **Показать** replaces the snapshot; **Ещё 100 свипов** loads its next
page. Rendering stops at 1,000 source sweeps, with an explicit limit message.
Automatic refresh every five seconds is optional and replaces the snapshot;
collection remains in the core. Each chart mark spans the full source interval,
so the display never joins separate sweeps into continuous coverage. Axis labels
and source-table timestamps are UTC, independently of the Mac's time zone.

The marker action **История → Следить за уровнем маркера** and the selected
candidate action seed this view without tuning. **Открыть исходник** opens the
original archived spectrum. **Эталоны и сравнение** reuses the existing core-owned
comparison and reference catalogue; it performs no client-side subtraction.

Archive frequencies use the metadata contract range (0–9,007,199,254,740,991 Hz),
not a device tuning range. MHz editing and formatting retain exact integer Hz,
including frequencies above 800 MHz. This does not enable hardware tuning.

Saved sets are session-scoped. A portable JSON file contains only
`schema_version: 1`, `name`, and `entries`; import is limited to 8 KiB and rejects
unknown fields and commands. Saving a changed set creates a version; importing
or selecting one does not apply radio settings. Unsupported servers show the
missing history capability and keep existing acquisition controls available.


The Rust archive serves the same versioned frequency bookmark sets as the Python server. A reader can list the sets. Saving requires operator access on a paired node. The RF Explorer pass view can use them without a scientific worker. [RF Explorer pass review](rf-explorer-3g.md)
