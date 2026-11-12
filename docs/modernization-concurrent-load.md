# Concurrent background work on one replay Core

This trusted operator fixture composes the existing benchmark producer and
three passive Chromium clients with one Core backup, one live raw NDJSON export
and one real public History summary job. It adds no production HTTP route or
`ori-core` command, creates no instrument transport, and enables no native audio
device. Uploaded closed PCM remains part of the original producer workload.

The default workload remains 1,800 real seconds, 18,000 sweeps of 4,096 points,
18,000 receiver observations and 86,400,000 PCM frames in 180 closed WAVs. The
original producer ledger, queue, offered denominator, verification, 10-second
request timeout, 600-second bulk timeout and 60-second drain are unchanged.
`--smoke-seconds` explicitly selects a smaller functional exercise and cannot
establish the thirty-minute reference result.

## Build and trusted worker policy

Build the example from the same reviewed source as Core, then copy the executable
to an immutable operator-selected path outside the build cache. No build,
package installation, browser download or cache creation is performed by the
Python wrapper.

```sh
cargo build --locked -p ori-core --release --example benchmark_background
```

Supply an absolute installed research Python. The example uses production
`main.rs`'s exact trusted `WorkerSpec`: `PYTHON -I -m
radioinspector.research_rpc`, empty explicit environment, 30,000 ms deadline,
and either a delegated cgroup with hard memory required or an explicitly
selected best-effort policy. Core's existing History binding changes only the
worker module to `-I -m ori_research`. Missing worker/containment is an error,
not a scientific stub or an automatic fallback. The report records the selected
executable SHA-256, argv, policy and the actual History result's enforcement.

```sh
ORI_PLAYWRIGHT_MODULE=/absolute/installed/playwright/index.mjs \
ORI_BROWSER_EXECUTABLE=/absolute/installed/chromium \
python -m benchmarks.ori_bench.concurrent \
  --candidate /absolute/immutable/benchmark_background \
  --analysis-python /absolute/research-venv/bin/python \
  --research-cgroup /absolute/delegated/research-cgroup \
  --observer-node /absolute/installed/node \
  --output-dir /new/disposable/concurrent-001
```

On a host without the delegated hard limits, the operator may instead explicitly
select `--research-best-effort`. That choice is recorded and does not establish
hard memory or physical I/O enforcement. The existing observer uses the supplied
local Playwright/browser installation and its existing explicit loopback consent
and owned-project selection. It never arms a project or starts a device worker.

## Ownership, bounds and evidence

The wrapper uses the existing `OwnedCandidate`/`OwnedCommand` owner, readiness
validation, immutable binary check, private nonce stop request and stdin EOF
lifeline. Its narrow fixture-profile hook selects the example instead of the
production CLI. A validated private marker delivers exactly one `B` byte to the
example's stdin; another byte is rejected. The example owns one replay Core,
normal loopback listener, bounded stdin reader, original `BackupCancellation`
and retained backup task. EOF requests shutdown/cancellation and all original
backup, serving, input and Core cleanup work is JOINED before success. Failed
cleanup never becomes a graceful-close receipt.

At the default midpoint, measured from the producer's first committed source,
the controller requests one backup and opens one raw export. After their actual
receipts/data are observed, it submits one `POST /api/v1/history/jobs` summary
request over the first minute of original sweep timestamps. It polls the
existing public job/status/result paths; it computes no scientific result and
issues no SQL. Source size is bounded by the existing History policy.

The live export streams the existing session `archive/export.ndjson` path with
an 8 GiB total bound, 16 MiB frame bound and the original bulk socket timeout.
It verifies every source ID, integer Hz, raw unit, UTC interval and f64 bits with
the original producer's `measurement_hash`. It requires declared stable boundary
and count headers, rejects duplicate/missing/partial records, and records only
counts, maximum frame size and the actual NDJSON SHA-256. Its midload snapshot
does not claim all final planned inputs; the unchanged producer separately
verifies those inputs after production.

Passing overlap requires an observed running backup receipt, a raw cursor that
has delivered a verified original, a running real History job, and actual shared
QoS diagnostics with at least three registered owners and no pressure/cleanup
failure. Three submitted requests alone cannot pass. The bounded report records
source boundaries, operation windows, phase transitions, worker enforcement,
QoS samples and background errors. Missed or unavailable overlap stays failed
or unmeasured, including short smoke runs. No operation is repeated to manufacture
an overlap sample.

After producer verification and all background work, the original Core closes
through its owner. A separate owned invocation of the same example then calls
public `verify_backup` and `restore_backup` into a new, separate database path;
it opens no Core, listener or instrument. Manifest/SHA/count equality and process
JOIN are required. There is exactly one backup bundle and one restored database,
with no periodic copies and no deletion of raw data. Reserve sufficient space
for the original archive and both copies. Different paths on the same host do
not prove independent physical media.

`report.json` is the composed result; `producer/report.json` and its original
ledger retain the unchanged source evidence. All runtime artifacts must be in a
new directory outside Git worktrees. The original managed adapter and descendants
can be sampled, but Core-only RSS, Pi acceptance, physical RF/hardware operation,
and physical backup-carrier independence are not inferred from that tree.

## Verification status

Python source/AST and corruption/ownership-contract unit tests can be checked
without a Core build:

```sh
python -m unittest benchmarks.tests.test_concurrent \
  benchmarks.tests.test_endurance benchmarks.tests.test_benchmarks
```

The Rust example still requires the coordinated actual all-target build and
owned-process functional run. The complete thirty-minute concurrent workload,
Pi acceptance and physical hardware/carrier acceptance are **not run** until
their actual reports are observed. A source review or small functional run
cannot substitute for that evidence.
