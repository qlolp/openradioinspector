# Guard selection and scientific snapshots

These `ori-archive` queries run on the existing bounded read actors, inside one
read-only SQLite transaction per call. They do not open devices, mutate source
rows, run the detector, or duplicate Python scientific algorithms. The service
still owns authorization and durable state-machine decisions.

## Guard source metadata

`baseline_selection(session, stream)` returns `BaselineSelection`:

```text
sweep_ids: Vec<String>                 // at most 256
count: u64                            // all scoped indexed sweeps
first_started_at: Option<String>
last_ended_at: Option<String>
elapsed_seconds: f64
sweep_period_seconds: Option<f64>
```

Selection follows the actual SQLite-backed
`PreparationOperations.baseline_samples`: retain the latest twenty, use the
nearest source at or before the latest selected end minus sixty seconds, and
seek the first source at or after each of 236 uniformly spaced target times.
Deduplicate by exact ID and return the existing Python ordering. Fewer than
twenty sources have no cadence; otherwise cadence is the median of positive
adjacent periods in the latest twenty, with the existing one-second fallback.
Collection count/span use the same `monitoring_sweep_index` join and MIN/MAX as
Python preparation. The caller applies the twenty-source/sixty-second readiness
rule and loads each selected typed Sweep separately.

Only IDs and scalar capture metadata are read, never 256 raw spectrum arrays.
The implementation preserves the authority's legacy details: recent selection
uses `list_sweeps`' raw-time expression and native SQLite ID ties, uniform seeks
use the Python SQL ID ordering, and the final full selection uses UTC time then
Python string order. UTF-16 fixtures and historical offset text are not rewritten
or silently reordered into a different baseline. Uniform target arithmetic
matches Python `timedelta * float`: exact binary64 rational multiplication and
nearest-even integer microseconds. Cadence/span division also preserves Python's
correctly rounded integer-microsecond result.

`capture_positions(ids: &[String])` accepts up to 256 IDs and returns a map of
found IDs to `CapturePosition { id, rowid, session_id, stream_id, started_at }`.
Missing IDs are absent; duplicates collapse in the map. Returned ownership lets
the service apply baseline-reference boundaries when a source is missing or
belongs elsewhere, as `BaselineOperations.detection_baseline` does.

`legacy_pending_sweeps(session, stream, after_rowid, limit)` returns at most 64
`CapturePosition` values in increasing **`sweeps.rowid`** order. The cursor is the
existing project `detector_rowid`. It is not `sweep_ingestion.sequence`: a v5
ingestion backfill orders IDs and can disagree with original capture ROWIDs and
their holes. `capture_positions` also supplies the actual maximum selected
ROWID when a new baseline initializes that cursor. Existing ingestion APIs
retain their separate semantics for archive summaries and capture paging.

`open_events(project, session, baseline)` returns at most 1000 exactly scoped
open events. `pending_receiver_events(project, session)` returns at most one
event using the existing eligibility predicates and last-seen/ID ordering.
It does not add an age cutoff or open-state filter: Python must still mark a
selected stale event expired, and its original query can select a closed event.

`event_sources(project, event, after_id, limit)` validates event ownership and
returns `RecordPage { items, next_cursor: Option<String> }`, with a limit of
1–64 and a one-row lookahead. Cursors and IDs are exact strings. Foreign or
missing events return `NotFound`; evidence from another project is excluded.
All record reads retain the existing response-byte and SQL-deadline budgets.

`position_marks(project, search_session, limit)` returns 1–256 latest scoped
marks. An optional search-session predicate is applied in SQL before ordering
and LIMIT, matching Python `search_status`; marks for an active search cannot
be hidden behind a thousand newer marks from other searches. An absent search
scope returns the project's latest marks across searches.

## Scientific request snapshots

```text
analysis_inputs(session, Vec<sweep_id>, Vec<candidate_id>)
  -> (session DTO, Vec<sweep DTO>, Vec<candidate DTO>)

analysis_context(session, anchor_id, before, after)
  -> Option<(session DTO, SweepContext)>
```

`analysis_inputs` accepts at most 33 sweep IDs and 1000 candidate IDs, preserving
requested order and duplicates among found same-session sources. Missing or
foreign sources are omitted, while a missing session returns `NotFound`.
The caller forwards the original analysis payload unchanged to the pure Python
RPC; its existing validators then decide the exact missing-ID error and retain
validation priority. No foreign DTO is returned.

`analysis_context` captures the session and actual nearest same-stream neighbors
under that same read snapshot, with at most sixteen on each side. A missing or
foreign anchor returns `None`; a missing session returns `NotFound`. Scientific
compatibility is deliberately left to the original Python method. Raw units,
settings and frequency grids are returned even when they are incompatible;
farther compatible sources never replace actual nearest neighbors. The existing
public `sweep_context` keeps its earlier validation/dBm behavior.

The complete session-plus-source tuple shares one configured response budget
(16 MiB by default), including repeated sources and framing. It never combines
independent reads from different commits. Archive JSON decoding and source
precision remain unchanged; no derived values are persisted by these queries.

Summary query frequency filters accept every nonnegative `i64` bound, including
values above the raw-write frequency limit. Negative bounds and reversed ranges
are rejected. Raw-source write validation remains unchanged. Artifact metadata
offsets retain the existing HTTP ceiling of 10000000.

## Synthetic verification

`crates/ori-archive/tests/generate_query_fixtures.py` generates UTF-8 and UTF-16
owned v5 databases and expected values by calling the actual Python preparation,
baseline and project-store operations. Cases include over seven hundred high
cadence sources, ROWID holes, offset text, tied/non-BMP IDs, zero cadence,
century-scale intervals, event scope/eligibility and evidence pages. A separate
2596-vector oracle checks Python microsecond target/seconds rounding.

Rust regressions compare the compact results with those oracles, verify that
metadata selection never decodes raw arrays, distinguish ROWID from migration
ingestion order, enforce response/count/cursor limits, and race scientific
snapshots against atomic writer commits. The fixtures contain no real hardware,
credentials or private measurements. Focused crate tests and clippy/fmt are run
after benchmark quiet periods; the root integration owns broader release gates.
