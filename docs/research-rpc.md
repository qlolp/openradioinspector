# Pure scientific RPC

`radioinspector.research_rpc` runs one bounded scientific request and exits. It
uses the existing Python `SessionService(store=None)` methods and the controller's
host-only TinySA scientific function, plus the original pure automatic-detection
function, without duplicating their algorithms.
It never constructs an archive, recovers sessions, creates
device workers, opens ports, records audio, or writes results/events. The Rust
core owns authorization, the archive read snapshot, process admission and
cancellation, and any later explicit persistence of a derived result.

## Wire and process contract

Start the installed root library with `python -I -m radioinspector.research_rpc`.
Write one UTF-8 JSON document to stdin and **close stdin**. There is no line-based
request loop. Input and the complete output, including its terminating newline,
are each limited to 16 MiB. Reads are at most 64 KiB; output is validated and
bounded before any response bytes are published. Success exits 0, a structured
error exits 1. A cancelled/closed output pipe cannot receive a response.

Before spawning, set `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`,
`NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`, and `ARROW_NUM_THREADS` to `1`.
The worker also sets these before its lazy service imports. The parent setting
is required for `-m`: the existing `radioinspector.__init__` eagerly imports NumPy
before Python executes the submodule. Running the installed `research_rpc.py`
file directly also works and sets the environment before package imports.
The root wheel must be installed in the isolated worker environment; this
module does not vendor algorithms or depend on the DuckDB research package.

For the original fourteen service methods, the input object has exactly these
fields, plus optional `limits` and the trusted-supervisor-only `shared_io`
capability described below:

```json
{
  "version": 1,
  "request_id": "caller-correlation-id",
  "operation": "analysis",
  "method": "compare_sweeps",
  "session": {"existing": "MonitoringSession DTO"},
  "sweeps": [],
  "candidates": [],
  "payload": {"reference_id": "exact-reference-id", "target_id": "exact-target-id"}
}
```

The illustrative session placeholder must be replaced with an existing full
session DTO, including its ID, creation time and streams. Sources are existing
full Sweep/Candidate DTOs with explicit IDs, intervals, ownership and raw units.
There are no generated IDs, wall-clock timestamps, or new ingestion events.
If the parent's same archive snapshot contains no requested session, it may
send `session:null` with `session_id` holding that exact requested ID and both
source arrays empty. The worker seeds no session and calls the same method, so
malformed payloads retain the existing method's validation-versus-not-found
error order. A full session DTO must not also contain this envelope field;
missing-session envelopes cannot carry sweep or candidate sources.
The correlation ID is a nonblank UTF-8 string of at most 128 characters. JSON
duplicate keys, non-finite values (including overflowing exponents), invalid
UTF-8/surrogates, unknown envelope fields, operations and methods are rejected.
The envelope accepts no SQL, paths, URLs, command text or module selectors.
Archived metadata, labels and stream descriptions remain inert data.

Up to 33 sweeps and 1000 candidates may be supplied. Ownership and duplicate
identities are checked before the memory service is seeded. Frequency and level
arrays must have matching nonzero lengths, frequencies must be integer Hz, and
levels/optional candidate levels and scores must be finite JSON numbers, without
boolean/string coercion. Raw unit strings and scope identities that would need
silent normalization are rejected. Source IDs, numerical values (including
binary64 negative zero), settings and metadata are never rounded or recalibrated.
Intervals use the existing timezone-aware DTO parser and existing UTC result
formatting, preserving the represented instant and microseconds.

Existing method validators remain authoritative: comparison-family methods
allow at most 16384 points, averaged references 2–16 sources, and context at most
131072 selected points. Full context can contain 33 sources. A 16-reference
average plus its target can contain 278528 points. Frequency-counter has no
legacy 16384-point cap, so this wrapper does not add one; its conservative total
point bound is `16 MiB / 4`, derived from the minimum wire size of two arrays.
Limits are errors, never truncation, sampling or a partial scientific result.

Optional `limits` recognizes exactly the ten `ori_research.protocol` v1 fields:
`duckdb_threads` (2), `duckdb_memory_bytes` (512 MiB), `worker_rss_bytes` (768 MiB),
`cache_bytes` and `temp_bytes` (10 GiB each), `io_bytes_per_second` (20 MiB),
`batch_rows` (1024), `batch_bytes` (4 MiB), `max_record_bytes` (8 MiB), and
`max_points_per_sweep` (65536). Each supplied value must be an integer from 1
through its default. These fields provide compatibility with the shared process
pool; this point-analysis worker has no DuckDB/cache/batch resource accounting
and does not reinterpret scientific limits through those fields. The parent
must enforce process-tree memory/time/I/O limits, share heavy-job admission,
bound both pipes, kill/reap the child on cancellation, and validate correlation
and operation before exposing its result. This pure subprocess is not an OS
sandbox for arbitrary Python code.

## Source selection and method allowlist

All methods receive the original validated HTTP analysis payload as an object;
GET query values for comparison/threshold become the typed keyword fields below.
Imported JSON artifacts stay JSON **strings**, never paths. Methods not listed
here cannot be dispatched, including session transitions, audio, imports,
measurement-result writes, and all instrument actions.

| Method | Payload fields | Sources required from the caller's consistent snapshot |
| --- | --- | --- |
| `compare_sweeps` | `reference_id`, `target_id` | Both exact IDs; one source when equal |
| `sweep_average_comparison` | `reference_ids`, `target_id` | Distinct 2–16 reference IDs, separate target |
| `sweep_average_change_detection` | `reference_ids`, `target_id`, `minimum_increase_db`, `minimum_bins` | Same as average |
| `sweep_threshold` | `sweep_id`, `window_points`, `margin_db` | Exact sweep |
| `sweep_three_thresholds` | `sweep_id`, `levels_dbm` | Exact sweep |
| `sweep_context` | `sweep_id`, `before`, `after` | Exact anchor and actual nearest same-stream neighbors |
| `sweep_occupancy` | `sweep_id`, `before`, `after`, `threshold_dbm` | Same exact context snapshot |
| `sweep_mask_analysis` | `sweep_id`, `mask_json` | Exact sweep |
| `candidate_frequency_rounding` | `candidate_id`, `profile_json` | Exact candidate |
| `frequency_rounding` | `candidate_ids`, `table_json` | Exact selected candidates, 1–256 |
| `sweep_quality` | `sweep_id`, `edge_margin_db`, `high_level_dbm` | Exact sweep |
| `frequency_counter` | `sweep_id`, `resolution_hz`, `start_hz`, `stop_hz` | Exact sweep |
| `dvb_evaluation` | `parameters`, `thresholds`, `guard_interval`, `code_rate` | Session only |
| `measurement_map` | `map_json` | Session only |

For context and occupancy, before/after are each 0–16. The parent must obtain
the **actual nearest** neighbors, ordered by UTC `started_at` then binary UTF-8
ID, under one archive snapshot, **before** checking compatibility. Supply an
incompatible nearest source so the existing algorithm rejects it; never replace
it with a farther compatible source. The worker only sorts/selects supplied
sources and cannot establish that a missing neighbor does not exist in the
archive. It reports the existing `missing`/`complete` fields without inventing
history. The parent also owns archive validation of candidate observation-ID
provenance: observations are not part of this transport.

## Private automatic-detection scientific method

The same trusted worker accepts one separate fixed method under the existing
`analysis` operation. Its closed envelope has exactly seven required fields,
plus optional `limits` and trusted `shared_io`:

```json
{
  "version": 1,
  "request_id": "owned-detection-correlation-id",
  "operation": "analysis",
  "method": "automatic_detection_regions",
  "sweep": {
    "id": "existing-sweep-id",
    "session_id": "existing-session-id",
    "stream_id": "existing-spectrum-stream-id",
    "interval": {
      "started_at": "2026-11-11T12:00:00.123456Z",
      "ended_at": "2026-11-11T12:00:00.223456Z"
    },
    "frequencies_hz": [100000000, 100100000],
    "levels": [-90.0, -50.0],
    "level_unit": "dBm",
    "settings": {}
  },
  "threshold_dbm": -60.0,
  "maximum": 10
}
```

The worker calls the unchanged
`radioinspector.automatic_detection.detect_regions(Sweep.from_dict(sweep),
threshold_dbm=..., maximum=...)`. This is the original function called by
`AutomaticCandidateDetector.run_once`: contiguous regions at or above the
threshold, ordered by strongest peak and then frequency, with its original
single-bin bandwidth fallback. There is no replacement threshold calculation.
It constructs only the existing Sweep DTO, without a service, detector,
scheduler, archive, store, device, candidate or investigation.

The threshold must be a finite JSON number in `-300..100` and the maximum an
integer in `1..100`, matching the existing policy bounds. A source needs its
explicit ID, session and stream IDs, ordered UTC interval (`Z`, zero to six
fractional digits), original `dBm` unit, nonempty matching arrays of
nondecreasing unsigned 64-bit integer Hz and finite raw levels. Duplicate
frequencies and one-point sweeps remain legal, as in the original algorithm.
Boolean/string numeric coercion and scope IDs requiring trimming are rejected.
Settings, when supplied, must be an object. Opaque source extensions remain
inert; the parent retains the original source and its provenance. The existing
16 MiB frame bound and conservative `16 MiB / 4` point ceiling apply, without
resampling or truncation.

The ordinary five-field success envelope has `operation:"analysis"` and
`result` is a list of the original four-field regions:
`{center_frequency_hz,bandwidth_hz,peak_level,score}`. A trusted capped success
adds only the existing outer zero-file-I/O receipt. Malformed input is
`protocol_error` / 400; shared-I/O failure is `shared_io_unavailable` / 503,
before source construction or math. Existing scientific failures use the
ordinary error mapping. Final output, including any private receipt, must fit
16 MiB. Existing fourteen-method service envelopes and five control actions
are unchanged.

The parent captures and retains the original authorization, session/worker and
Guard generations before submission, admits this work through the same shared
ResearchPool, and revalidates after actual child/pipe JOIN and before any
candidate insertion or other effect. The worker receives no authority receipt
and cannot acknowledge those checks or perform a handoff/tune itself.

`tests/fixtures/automatic-detection-science-v1.json` freezes eight synthetic
outputs from the original function, including tied peaks, threshold boundaries,
duplicate-Hz fallback, an empty result, negative zero/subnormal levels and
integer Hz above binary64 precision. Tests compare these with live original
calls, an actual memory-only `SessionService` and `AutomaticCandidateDetector`
oracle, and eight owned source subprocesses. They also verify exact source
identity/settings/UTC, validation before source construction, no service/store
construction, private zero-I/O receipts and final response bounds. These are
synthetic scientific checks; hardware validation remains `not_run`.

## Private control scientific action

The same trusted worker and ResearchPool accept a separate fixed action:

```json
{
  "version": 1,
  "request_id": "caller-correlation-id",
  "operation": "control_analysis",
  "method": "peak",
  "source": {"existing": "owned RawTrace document"},
  "rbw_hz": 10000.0
}
```

Methods are exactly `peak`, `occupied_bandwidth`, `bandwidth_3db`, `centroid`
and `channel_power`. `source` is the existing complete raw document with exactly
`frequencies_hz`, `levels`, `level_unit`, `wire_base64`, `zero_db`, `started_at`,
`ended_at`, `sample_offsets_seconds`, `timing_source`, `grid_source`,
`requested_stop_hz` and `stop_is_inclusive`. It carries 2–65536 strictly increasing
unsigned integer Hz and the same number of finite raw levels, with `dBm` units,
finite zero context, valid base64 evidence and an ordered UTC interval at up to
microsecond precision. Current timing is `interval_only`, sample offsets are
null, grid provenance is `firmware_ea71dfd_scanraw`, and stop inclusion is false
or null. The requested stop is an unsigned integer Hz or null. Actual read-back
RBW must be finite and positive; the worker never substitutes an estimate.

The existing `radioinspector.tinysa.measurements.analyze(source, method,
rbw_hz=...)` owns the full calculation and existing warning/uncertainty shape.
No controller, adapter, port, session/store, artifact or canonical Sweep is
constructed. The caller retains the original raw document and attaches it to
its existing artifact after validating the derived-only result. Source values,
wire evidence and UTC strings are not rewritten. This action admits optional
lowering-only `limits` and trusted `shared_io` as above, with zero file-I/O
counters. It is never a public module selector or RF command.

Control success/error envelopes use `operation:"control_analysis"` and the same
five fields as ordinary analysis; a private capped success adds only the outer
receipt. Invalid shape/context/method is `protocol_error` / 400, existing
scientific validation is `validation_error` / 400 and unavailable shared I/O is
`shared_io_unavailable` / 503. The complete request/response ceiling remains
16 MiB. The parent allows that ceiling for exactly the two private scientific
operations; DuckDB's smaller request/response limits are not widened. The same
pool, cancellation, child/pipe JOIN and original owner-receipt gate apply.

The synthetic fixture `tests/fixtures/control-science-v1.json` records the
scientific subset and SHA256 of the existing 36-case actual PythonController /
UltraWire oracle. Tests also invoke that controller on owned in-memory wire
responses for all five methods, then compare exact encoded/f64 results with
the RPC and frozen fixture. Source-bootstrap Python 3.15.1 verification passed
all 41 Legacy/point/control RPC tests, including five actual owned control
children, malformed-input no-dispatch/no-mutation, negative zero and Unicode
identity, private zero-I/O receipts and final output bounds. No RF, serial or
physical accuracy/performance acceptance is implied.

## Result and error envelopes

Success is `{version:1, request_id, operation:"analysis", ok:true, result:...}`;
`result` is the exact existing method result serialized through `to_primitive`.
It does not contain an additional HTTP `data` envelope.

Errors replace `result` with `error` and set `ok:false`. `error.code` is one of
`protocol_error`, `resource_limit`, `validation_error`, `not_found`, `incompatible`,
`shared_io_unavailable`, or `internal_error`. `type` and `message` preserve the exact expected service
exception class and `str(exception)`. `api_code`, `api_message`, and `http_status`
carry the legacy HTTP mapping so the core can retain its existing API response.
In particular, missing-source `KeyError` messages include Python quoting in
`message`, while `api_message` uses `exception.args[0]`, as the existing HTTP
handler does. Unexpected failures use a generic internal error without a
traceback or source record. The response correlation ID is null when an invalid
or incomplete request cannot supply a validated ID. Oversized output becomes
a bounded `resource_limit` envelope; an algorithm never returns partial data.

## Private shared file-I/O capability

After public request validation, the supervisor may add a top-level
`shared_io:{version:1,path,nonce,clock:"posix-monotonic"}`. This is the existing
128-byte owner-private ledger capability; `path` is absolute and `nonce` is 32
lowercase hexadecimal digits. Clients cannot select or forward this field.
The worker opens and validates it after protocol validation and before lazy
service imports. Malformed, unsupported or unavailable capabilities fail with
`shared_io_unavailable` / HTTP 503, without falling back to an independent
timeline. Capability paths and nonces never appear in a receipt or error.

Only a successful capped request adds the outer field
`shared_io_receipt:{version:1,accounted_bytes,charges,max_charge_bytes}`. These
unsigned counters describe file-payload reservations, not physical disk I/O,
RAM or stdin/stdout transport. Point, control and automatic-detection science
open no application files or caches, so their three counters are zero. The shim's zero-byte admission check
does not count as a data charge. Ordinary requests retain the exact original
five-field response. The complete encoded frame, including a private receipt,
still must fit 16 MiB. Errors have no success receipt.

The supervisor requires and validates the receipt only when it supplied the
capability, then removes it before the existing five-field scientific/legacy
result validator. Without that trusted handoff an extra field remains invalid.
The parent retains the shared permit through actual child and pipe JOIN; receipt
bytes alone do not acknowledge process cleanup. See [the common ledger
contract](shared-background-io.md) for its fixed 20 MiB/s timeline and controls.

## Verification

The tests use synthetic measurements and compare all fourteen results and
validation errors with direct memory-only `SessionService` calls. They cover
Unicode/exact IDs, UTF-8 neighbor ordering, intervals, original settings, float
bits and negative zero, incompatible/raw units and nearest settings, stale or
foreign sources, full context/average limits and a larger counter grid, duplicate
JSON/IDs, non-finite and malformed/truncated input, limits, short reads, atomic
output limits, closed pipes and cancellation. SQLite constructors/connections
are trapped during the memory-only check; events/results/investigations remain
empty. Every method also runs through a real one-shot subprocess.

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python -m unittest tests.test_research_rpc -v
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 /workspace/.venvs/ori-research/bin/python -m unittest tests.test_research_rpc -v
```

Both commands run the same tests without interpreter-version skips. The second
command exercises actual Python 3.15 subprocesses against the source checkout;
the release profile separately verifies the installed root wheel with `-I`.
The detection-method source gate passed all 48 Legacy/point/control/detection
RPC tests on Python 3.15.1, including the seven new detector checks. This source
gate does not claim that a previously installed worker wheel has the new method.
No physical hardware or private measurements are used in these checks.
