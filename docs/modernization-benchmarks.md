# Reproducible modernization benchmarks

`benchmarks.ori_bench` is an opt-in, standard-library Python HTTP producer and
archive verifier. It creates a new synthetic session, offers inputs against a
wall-clock schedule, and verifies original measurements and audio through public
API reads. Its reports describe the measured host and workload. A successful
small run is not Raspberry Pi, instrument, UI, million-sweep, 30-minute, or
seven-day acceptance.

Harness verification used Python 3.15.1 on Linux x86_64. The optional controlled
browser fixture used Node 26.13.0, Playwright 1.62.1 and Chromium 156.0.8078.79;
these are tested runtime patches, not a claim about a deployed client.

Use an explicitly supplied core URL backed by a **disposable benchmark-owned
archive**. The harness does not launch, stop, reconfigure, or delete a core. It
cannot establish a server's database pathname through HTTP. Its enforceable
write boundary is a newly created session: an existing run ID is rejected, and
no ingestion begins unless creation returns 201 with the requested session and
synthetic stream IDs. It never starts workers, selects ports, issues RF commands,
or deletes source data. Do not point a stress producer at a production archive.
When the core advertises monitoring guards, bounded read-only project/status
queries must show no active guard or busy receiver before any session is
created. The harness never arms, disarms, or changes a guard. This is a
point-in-time preflight check; unadvertised guard state remains unmeasured.

## Commands and workload shapes

Run from the repository root with a user-owned Python 3.15 interpreter; there
are no benchmark Python dependencies to install. All output directories must be
new and outside Git worktrees. Existing directories and files are preserved.

Small actual-core smoke test, after starting a dedicated test core yourself:

```sh
python -m benchmarks.ori_bench run --base-url http://127.0.0.1:8765 \
  --output-dir /tmp/ori-smoke-001 --run-id smoke-001 \
  --duration-seconds 1 --audio-segment-seconds 1 --points 4096 --history-repeats 3
```

The default full stress scenario uses real elapsed time:

```sh
python -m benchmarks.ori_bench run --base-url http://127.0.0.1:8765 \
  --output-dir /tmp/ori-stress-001 --run-id stress-001 \
  --pid core=1234 --sample-interval 0.5
```

Its exact offered workload is 1,800 seconds, 10 sweeps/s × 4,096 points,
10 receiver observations/s, and complete 48 kHz mono PCM16 audio in closed
10-second WAV segments: **18,000 sweeps, 18,000 observations, 180 WAVs,
86,400,000 frames / 172,800,000 PCM bytes**. WAV headers are retained separately
within each original artifact. HTTP JSON audio bodies are approximately 1.28 MB
per 10-second segment. The final segment may be shorter for a custom duration;
the last PCM frame is still offered. Audio boundaries must be exact complete
frames representable by the configured microsecond times.

The complete plan gate also needs three actual clients and externally started
research/export/backup background work on that disposable node. The producer
does not launch those jobs or infer their activity from a PID. Record their
process roots and arrange them through the owning supervisor; background
concurrency is otherwise unmeasured.

Change duration/rates/points with `--duration-seconds`, `--sweep-rate`,
`--observation-rate`, and `--points`. `--speed` accelerates the schedule only;
it does not simulate a faster core or establish a real-time stress result.
`--workers` defaults to 2 and `--queue-capacity` to 64. The scheduler records an
offer before nonblocking enqueue, independent of acknowledgements. A full queue
records a drop and continues offering; it never reduces the offered denominator
to make delivery look complete. Drain and HTTP deadlines are configurable.

Explicit overload and fault scenarios:

```sh
python -m benchmarks.ori_bench run --base-url http://127.0.0.1:8765 \
  --output-dir /tmp/ori-overload-001 --run-id overload-001 --scenario overload \
  --duration-seconds 30 --sweep-rate 1000 --queue-capacity 2 --workers 1

python -m benchmarks.ori_bench run --base-url http://127.0.0.1:8765 \
  --output-dir /tmp/ori-fault-001 --run-id fault-001 --scenario fault \
  --duration-seconds 30 --fault-kind invalid-batch --fault-every 10
```

`invalid-batch` offers a deliberately empty sweep array at each selected index;
the whole request must be rejected. `client-gap` deliberately does not send
selected offered sweep requests. Neither injects a hardware or filesystem fault.
Transport timeouts remain **unknown delivery**, since a server may commit after
the connection is lost; export verification determines whether the source was
retained. A rejection, client drop, or unknown acknowledgement is visible in
the ledger and report. An expected fault/overload run can exit 0 with
`complete_delivery.status: failed`; that exit means the explicit experiment
finished with consistent accounting, not that every offered source was archived.
Invalid archive content or an invalid acknowledgement still fails the run.

The separate accelerated source-time scenario is:

```sh
python -m benchmarks.ori_bench run --base-url http://127.0.0.1:8765 \
  --output-dir /tmp/ori-source-week-001 --run-id source-week-001 --scenario lifecycle
```

It spans seven logical source days at 86,400× schedule speed, with an hourly
sweep, observation, and 10-second audio segment: 168 of each. This deliberately
sparse replay exercises long timestamp spans and source ordering. It does not
offer seven days of continuous audio, restart the service, advance its clock,
or establish seven-day service endurance. `seven_day_service_endurance` remains
unmeasured. Service restart/cutover/disk-full and physical fault orchestration
must be performed by an external owner of the disposable core.

## Million-sweep history fixture

Preload one million × 290-point sweeps through bounded typed HTTP batches,
then prime and time the first 100 summaries repeatedly, and stream-verify every
retained source. This is an explicit, potentially many-gigabyte and long-running
operation, never a default PR test:

```sh
python -m benchmarks.ori_bench history --base-url http://127.0.0.1:8765 \
  --output-dir /tmp/ori-history-million-001 --run-id history-million-001 \
  --sweeps 1000000 --points 290 --allow-million --batch-size 16 --history-repeats 20 \
  --pid core=1234
```

Use `--sweeps 11 --points 290 --batch-size 3` for a small functional test.
The harness negotiates the server's batch item and byte limits. It holds at
most one bounded batch, one bounded NDJSON line, and one bounded audio segment;
it stores offered metadata in a disk-backed SQLite ledger instead of keeping a
million IDs, arrays, acknowledgements, or raw latency samples in memory.

When `features.sweep_summaries` is true, the measured route is
`GET /api/v1/sessions/{id}/sweep-summaries?limit=100`. The first request is reported
separately; subsequent requests form the warm histogram. Summary IDs, intervals,
units, point counts, integer-Hz bounds, original peak float bits, first peak tie,
and newest-source ordering are checked. Raw arrays in a summary are an error.
Without the capability, full-measurement `/sweeps?limit=100` latency is recorded
under a different metric and the summary target remains unmeasured. The 500 ms
summary target is only graded when the complete million × 290 fixture matches;
small configurations never pass that reference gate. The host is still the
actual measured host, and `pi_acceptance` remains unmeasured.

## Verification and report interface

Every input ID and payload is deterministic for `run_id`, workload configuration,
and source algorithm version. Integer-Hz arrays include a one-Hz offset; source
times use UTC microseconds; sweeps include negative zero and adjacent float64
values. Receiver counts retain `s_meter_count`, without a dBm conversion. PCM
uses a deterministic absolute-frame sequence, including segment boundaries.

Required capabilities are persistent archive, typed batch ingest and streaming
NDJSON export; `run` additionally requires `audio_segment_ingest`. A missing
audio capability fails as `unsupported` before session creation. There is no
skip or fallback that marks audio as preserved. An optional bearer token may be
provided through `ORI_BENCH_TOKEN`; it is never written to reports. Redirects
are refused. Configure TLS trust normally; verification is not disabled.

The harness uses only these core HTTP operations:

| Purpose | Interface |
| --- | --- |
| Preflight | `GET /api/v1/capabilities`, `GET /api/v1/health`, `GET /api/v1/sessions/{new_id}` |
| Create owned source context | `POST /api/v1/sessions` with explicit synthetic IDs/settings, no ports |
| Measurements | `POST /api/v1/sessions/{id}/{sweeps,observations}/batch`, 201 `{items,count}` |
| Closed audio | `POST /api/v1/sessions/{id}/audio-segments`, complete WAV base64 plus original SHA/interval/frequency/mode |
| Verification | Session NDJSON export, first-100 history/summaries, receiver artifact list with offset/limit, artifact content GET |

Audio summaries must retain `metadata.sample_rate`, `frame_count`, `interval`,
`frequency_hz`, and `mode`, plus exact `sha256` and `size_bytes`. Downloaded WAV
bytes, mono PCM16/48 kHz format, frame counts, and concatenated source PCM hashes
are checked. Audio bytes remain in canonical artifacts; the harness only uses
a temporary verification file and removes it after each segment. NDJSON is
streamed and hashed without storing a second full archive. Duplicate, foreign,
changed, or missing source IDs do not become successful verification.

Runtime files in the owned output directory:

| File | Meaning |
| --- | --- |
| `owner.json` | Identifies this disposable report directory |
| `offers.sqlite` | All offered IDs/indices/times, delivery outcomes, verification and optional render deduplication; benchmark application ID, never a canonical archive |
| `report.json` | Atomically published report, also written for supported partial failure paths |

`report_version: 1` includes measured `host`, exact `workload`, endpoint label,
reference targets, separate `checks`, source `counts`, audio frames/hashes,
before/after event watermarks, fixed-memory latency histograms, resource samples,
and bounded gap examples. The full gap evidence stays in the ledger. Outcomes
distinguish `passed`, `failed`, and `unmeasured`; `run_status` distinguishes a
completed experiment, unsupported core, and failed run. No report claims Pi or
physical acceptance. Latency p95 is a conservative histogram bucket upper bound
with at most 2% bucket width; it is not an exact stored-sample percentile.

`--pid label=PID` may be repeated for core/research/supervisor roots. Linux
sampling discovers descendants and reports observed root RSS and summed tree
RSS. Shared pages are counted more than once, short-lived peaks can be missed,
and inaccessible/missing roots remain unmeasured. Without explicit roots only
the harness is sampled. Non-Linux process-tree sampling is unmeasured rather
than invented. The harness does not enforce cgroups or throttle production;
background I/O, memory limits and acquisition priority belong to the service
supervisor. Keep producer/report storage separate when measuring core disk I/O.

## Optional passive render observer

The client host must set `data-sweep-id` on a visible rendered element **after**
the corresponding draw completes. The observer cannot establish that ordering
on its own. It does not click, tune, start a worker, or modify the client data.
The optional Node/Playwright tool opens three 1920×1080 pages and records bounded
after-draw markers. Supply a local installed Playwright module and browser when
they are not discoverable normally; no packages are downloaded by the observer.

```sh
ORI_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs \
ORI_BROWSER_EXECUTABLE=/absolute/path/to/chromium \
node benchmarks/observe-render.mjs --url http://127.0.0.1:8765/ \
  --output /tmp/ori-render-001.ndjson --run-id stress-001 --clients 3 --duration-seconds 1900
```

Run the load in parallel against the same displayed synthetic session. Wait for
the observer to finish, then attach its evidence to the completed owned report:

```sh
python -m benchmarks.ori_bench.observe --report-dir /tmp/ori-stress-001 \
  --render-events /tmp/ori-render-001.ndjson
```

`run --render-events` can instead consume an already complete observation file.
The producer and browser must run on the same host wall clock; hostname hashes,
clock declarations and nonnegative deltas are checked. Each client must observe
every offered sweep, with no observer drops, before the render check can pass.
The metric is **offered-to-render**, a conservative upper bound for
committed-to-render when producer/browser clocks are shared. It is never
reported as a measured commit-to-render interval. Without valid evidence the
UI check stays unmeasured or failed. Native clients may emit this same bounded
NDJSON contract after actual drawing; native motion/fps and user-task latency
are not inferred from the browser adapter.

## Automated checks and acceptance limits

```sh
python -m unittest discover -s benchmarks/tests -p 'test_*.py'
ORI_BENCH_PLAYWRIGHT_TEST=1 ORI_PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs \
ORI_BROWSER_EXECUTABLE=/absolute/path/to/chromium \
python -m unittest discover -s benchmarks/tests -p 'test_*.py' -k playwright_observer
```

The tests use controlled real HTTP, temporary SQLite, real WAV serialization,
and an optional controlled canvas page. They test generators, exact data,
offered accounting, rejections/drops, source corruption, unsupported features,
bounded summaries/batches/histograms, output/ledger ownership, and observer
coverage. Passing them establishes harness behavior only. Actual-core smoke,
full 30-minute stress, million-sweep performance, external fault injection,
seven-day service lifecycle and physical Pi acceptance must be run and reported
separately. No complete accelerated source replay is treated as elapsed service
endurance or a substitute for the two-instrument physical gates.
