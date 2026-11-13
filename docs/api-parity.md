# Public API and CLI parity gate

`tools/modernization/api_parity.py` starts fresh owned loopback nodes, first
compares **Python against Python**, and only evaluates Core when that producer
sanity succeeds. The gate compares real committed HTTP responses and checks
rejected requests against committed event/session state. Source/spec inventories
describe coverage; they do not substitute for HTTP behavior.

An absent executable, explicit `candidate_route_unavailable` or HTTP503 is
**unavailable**, never a pass or a skipped test. The command returns nonzero
unless every evaluated candidate case and read-only CLI probe passes. Existing
Core limitations are preserved in reports rather than hidden by the harness.

## Run and ownership

```sh
python -m unittest tests.test_api_parity
python tools/modernization/api_parity.py \
  --producer-python /absolute/path/to/core-python \
  --candidate /absolute/path/to/immutable/ori-core \
  --candidate-fixture /absolute/path/to/immutable/parity_node \
  --candidate-source-root /absolute/path/to/core-checkout \
  --analysis-python /absolute/path/to/installed-315/bin/python \
  --profile public --artifacts /absolute/path/to/fresh-empty-artifacts
```

The `sources` profile keeps the original 20-case source/domain prefix and adds
four large-grid/integer-frequency cases. It is useful while individual bindings
are landing. Omitting or naming a missing candidate produces an explicit
unavailable result after producer sanity; it cannot make the parity gate green.
The documented script entry point also works with Python `-I` from outside the
checkout; private helpers resolve from the script's own tree. A real two-node
regression checks producer sanity, native exits and cleanup in that configuration.

Each node uses port zero, a private fresh archive/data directory and an owned
stdin lifeline. Python uses the actual `LocalCore`, HTTP handlers, SQLite store,
Session and instrument services. Only the tinySA transport is synthetic; it
refuses every physical device path. Fixture-controlled ingestion avoids random
background measurements. The optional trusted `parity_node` example embeds the
same Rust Core with only an owned in-memory tinySA transport. It accepts the
`synthetic://api-parity/` namespace and the literal committed UltraWire replies;
every physical port, foreign adapter and unspecified command is refused. Its
ordinary receive loop may request the fixture's literal three-point scan; this
does not establish acquisition or RF parity. Both services keep the original
device actor, typed controller, HTTP handlers, archive and writer lease.

`--candidate-fixture` changes only the HTTP node executable. The report retains
the production `--candidate` SHA and uses that production executable for all
three candidate CLI probes; it separately records the embedding executable SHA
and identifies HTTP results as a trusted synthetic embedding. A missing fixture
fails before process admission and cannot silently fall back to the production
node. Omitting the option continues to exercise the production CLI as before.
This embedding uses the existing Unix owned-stdin fixture backend, and refuses
to run on other platforms. Build and retain it from the same reviewed source as
the production executable (`cargo build -p ori-core --example parity_node`).
Synthetic embedding results do not certify the production native connector.

Core receives no `--allow-serial`, provisioning or service-installation option.
An explicitly supplied research interpreter selects
the existing best-effort worker configuration; this does not claim hard cgroup
enforcement or scientific acceptance.

Every HTTP/SSE connection closes, then EOF asks the node to drain and exit.
The harness waits for process and output-reader joins and readiness-file removal.
Forced termination, leaked readers, nonzero exit or output exhaustion fails
cleanup. Node data is retained on failure. No live archive, external service,
hardware port, webhook, audio input or installed supervisor is used.

Core capability fields describe existing routes and policy behavior: OpenAPI,
the `ardv1-monitor-v1` receiver profile, task list/create/cancel, and explicit
automation/scheduler opt-in. Automation policy audit events persist in SQLite;
the active policy and scheduler enablement reset on a cold restart. Their
`*_policy_persistent` fields therefore remain false. `explicit_serial_adapters`
tracks whether a trusted transport factory is configured; it stays false on the
ordinary production node without `--allow-serial` and becomes true in the
explicit synthetic embedding. Hardware validation remains `not_run` in both.

## Exactness and bounds

JSON rejects duplicate exact keys, nonfinite/overflowing numbers, invalid UTF-8
and unpaired surrogates. Comparisons preserve integer Hz, float64 bits including
signed zero/nextafter, original UTC microseconds and exact UTF-8 IDs/map keys.
Source intervals, metadata and measurement values are never normalized.
Two named diagnostic extensions are additive only on their own endpoints:
health's `backend: "rust-candidate"` and the scheduler's boolean
`execution_available`. Only candidate additions absent from the reference are
allowed; shared fields, deleted fields and other response extensions remain exact.
On capabilities, new feature names follow additive negotiation; their original
values remain in the trace and their actual behavior has separate client gates.
Existing feature values and missing features still fail comparison. Optional
`storage_mode` must equal the original `session_storage`. Candidate-only
diagnostics require a bounded typed schema, `release_accepted:false` and
`hardware_validation:"not_run"`; deleting or changing shared diagnostics fails.
Resource counts/byte limits require exact integers; the two named connector/RSS
flags require booleans. JSON booleans cannot stand in for numeric limits.
The optional boolean `history_worker_configured` records configuration only;
it never substitutes for actual worker execution or containment checks.
The optional background-QoS diagnostic has an exact bounded schema: at most
eight registrations, uint64 logical bytes, the fixed20MiB/s logical rate and
typed pressure/cleanup/cross-process flags. Physical-I/O enforcement and aggregate
completion must remain false; accepting diagnostics cannot establish those gates.
Raw-export diagnostics likewise require the exact typed reader, record and
timeout limits and the declared absence of whole-database copies, heavy-worker
permits and native-source-I/O accounting. Shared diagnostics still compare exactly.
The optional connector diagnostic must match its existing resource-limit flag.
The six-field acquisition-pressure diagnostic requires boolean running/pressure
flags, at most 32 active analyzers, bounded calibrating/delayed subsets and a
bounded printable error or null. It does not certify latency or physical I/O.
Capacity is checked against each owned read-only SQLite transaction's actual
`page_count * page_size`, including its maximum, remaining-space equation,
warning/full state and disabled automatic deletion. Only after both independent
proofs may database and remaining byte counts differ between implementations.
Raw responses and the per-node capacity proof flag remain in the artifacts.
OpenAPI additions pass only when the producer returns the exact committed
baseline and Core returns the exact generated runtime document. The gate also
recomposes that document from the baseline and reviewed extension, whose composer
rejects replacing baseline operations or schemas. Every response definition,
security rule, support annotation and baseline hash participates in comparison;
an arbitrary extra field, changed common response or reversed comparison fails.
This proves an additive contract document, not implementation of its routes.
Only declared service-assigned lifecycle-clock paths vary; array annotations
cannot match opaque object keys. Async protocol checks validate the original
operation/connection IDs before replacing those generated identifiers and their
monotonic admission timestamps with scoped comparison markers.

The single collected `instrument-async-protocol` fixture has a reviewed additive
Control contract. Its exact case ID, operation and `PROTOCOL` method opt in;
ordinary endpoints and generic comparisons do not. When Core enriches this
fixture, all 27 known fields must be present and coherent: the 17 bound non-audio
catalogue operations have `available:true`, both audio operations have
`available:false`, the pre-inspect state is unknown with false physical readbacks,
`hardware_validation:"not_run"` and integer-zero lease seconds, the freshly
acknowledged input inspect has false paused/restart flags, and terminal cancel
replay confirms the same current worker. These describe binding support and the
observed synthetic stages, never permission to execute lab/RF operations.

Only these additions at their explicit typed positions may be absent from the
Python reference. Shared fields keep exact keys, types, float bits and values;
new/missing operation names, unknown or partial additions, flipped availability,
physical claims, reversed comparisons and opaque nested data still fail.
Original raw response bytes remain in the traces. Each accepted field, its exact
value and contract name is retained as `validated_extensions`, including when a
separate scalar/message or canonical-state check fails. The small regression
fixture retains actual collected Python/Core wire values and pins its shared
unchanged catalogue by SHA-256; the initial float/message regressions remain RED
under this additive contract.

SSE validates original committed sequence IDs and the actual v1 envelope,
retaining named events and opaque JSON. NDJSON validates complete final records
and snapshot count headers, then compares original records regardless of JSON
object-key order. Partial EOF cannot appear as a successful complete export.
CLI deadlines also own inherited descendant pipes; Windows reuses the existing
release runner's private Job Object and a gated bootstrap before command launch.

The gate caps cases at256, each response at2MiB, aggregate trace bodies per node
at32MiB, output at256KiB, and SSE at16frames. Readiness/HTTP/async/close deadlines
are fixed; they are not performance acceptance. Reports retain complete bounded
synthetic request/response bytes, selected headers, hashes, comparison details,
CLI output, node exit/join proofs and gate source provenance. Existing artifact
directories are refused; reruns cannot overwrite prior failures.

## Coverage and inventory

The inventory retains the original six route-authority hashes, additionally pins
their SSE transport helper, and records complete reviewed seven-file snapshots
for the Python startup/SSE shutdown correction and bounded consumption before
forbidden analysis-query refusals. The latter consumes only correctly framed
bodies within the existing route limit and absolute deadline; query validation
still precedes JSON parsing, and Origin/Content-Type checks remain before reads.
The RMS extension adds one operation to the original 135; the ten legacy
operations and CLI inventory are unchanged. A fourth complete reviewed snapshot
pins the integrated RMS authority at `fb0c0e3471c976e57d3687d5520c1027ecbd7fb4`.
All seven hashes must match one snapshot together; mixing individually approved
files, an unknown edit or a missing file fails before any node starts. The report
records the matched revision and these actual source hashes. This pins the route
inventory and reviewed transport code, rather than every file in the repository.
Historical source snapshots remain recorded, but a producer without the new RMS
method cannot pass the extended public profile. The original source-only profile
remains available; unavailable routes never satisfy a required successful case.

The committed inventory records **118 Python v1 paths /136 operations**, ten
legacy `/api/*` operations, 17 `radioinspector` subcommands and ten console entry
points, with authority-source hashes. A changed authority fails before launch
until its inventory is explicitly regenerated and reviewed. The public profile executes176 cases:
source/domain flows, 20,000-point commit/history, receiver/candidate integer Hz
above JavaScript's safe range, valid JSON/NDJSON exports, bounded SSE, published
route rejection cases and typed202/poll/idempotency/conflict/terminal-cancel
protocol over a synthetic wire. The added synthetic RMS/CW source exercises a
successful calibrated result, nullable uncertainty and incompatible-chain refusal;
all three analysis requests must leave the archive/session state unchanged.
The positive case checks the physical example's 55 dBuV/m, calibration digest,
profile and original source identity/level. No-state-change checks accompany rejected
mutation cases. Successful coverage of a rejection path does not establish a
physical success path or every domain branch.

`GET /api/v1/monitoring-projects/audio-inputs` remains explicitly unrun physical
enumeration in the inventory. Positive RF, microphone, GUI, TLS/operator trust,
long-duration throughput and latency acceptance are separate. Python's published
OpenAPI's single-stream GET and candidate by-id alias now use the existing
read-only session services. Candidate and investigation by-id aliases decode the
final ID exactly once, including escaped slashes and distinct Unicode forms.
Missing streams return a typed404 rather than closing the connection. Regression
tests verify that these reads preserve float64 bits and committed event state.
The sweep-reference query documentation still differs from its handler; this
remaining baseline gap stays visible.

The CLI inventory includes legacy acquisition, analysis, files and GUI entry
points that remain available through Python. Their absence from `ori-core` is
not described as their deletion. Actual new-Core probes are limited to
`--help`, `--version` and live `--check-ready`; the same safe producer probes
also verify that readiness does not acquire a second archive writer or mutate
committed state. Provisioning, pairing and physical commands are never run.

## Observed candidate evidence

Against the immutable all-feature `e1a80b1` executable
(`f36f6378b8b04593074b32231240fa4b242a6ff35caece4a18cc1d61bd420c71`),
Python/Python171-case sanity passed. Core returned **139 passes,4 mismatches and
28 explicit unavailable cases**. All three nodes and nine read-only CLI commands
exited successfully and joined. This is an intentionally failing candidate gate,
not the result of later source changes or a claim that Rust can replace Python.

The mismatches are shared capability values/missing fields and three instrument
artifact error responses. Unavailable cases cover raw exports, automatic
policies, uploaded-PCM phonogram/live audio and instrument controls. Some cases
exercise rejection paths, so their eventual success alone cannot establish a
physical success path. Positive control tests require a supplied synthetic
transport; a standalone candidate with native transport disabled cannot claim
that configuration or physical readiness.

The former high-Hz/large-grid, missing-session precedence, Legacy workspace and
canonical OpenAPI cases now pass. Raw failed attempts remain outside Git,
including the earlier direct-script import failure. The script regression now
executes the real isolated entry point rather than relying on module imports.
Complete API/CLI compatibility, target performance and physical acceptance
remain separate release gates.
