# Owned Rust sampled lifecycle

`python -m benchmarks.ori_bench.endurance` runs the existing deterministic
benchmark lifecycle producer against an explicitly supplied Rust candidate in
replay mode, then restarts that same owned archive twice. It creates a real
draft Guard project and checks public history, source data and original event
identities. It never starts a device worker, selects a physical port, arms a
guard, requests a microphone, or changes the core's clock.

The default is an **accelerated seven-day sampled timeline**: 604,800 logical
source seconds, one sweep, one receiver observation and one closed ten-second
48 kHz mono PCM16 WAV each hour. The exact denominator is **168 of each source**,
80,640,000 PCM frames / 161,280,000 PCM bytes / **1,680 seconds of PCM**. This is
not seven continuous days of audio, real-time service endurance, RF coverage,
instrument acceptance or Raspberry Pi performance acceptance.

The original `run --scenario lifecycle` command, source algorithm, offered
denominators and public benchmark defaults remain unchanged. The new module
composes `Config`, `run_load`, `send`, `Ledger` and the existing audio/history
verifiers. It does not substitute a second producer or an HTTP emulator.

## Run an explicit candidate

Use an installed Python 3.10+ interpreter; the harness itself uses only the
standard library. Run from a source checkout. Output must be a new path outside
Git worktrees. The harness never removes the archive, previous reports or
failure evidence.

```sh
ORI_CORE_ENDURANCE_BINARY=/absolute/path/to/immutable/ori-core \
python -m benchmarks.ori_bench.endurance \
  --output-dir /tmp/ori-sampled-week-001 --run-id sampled-week-001
```

`--candidate` is an equivalent explicit path. There is no guessed default
binary. SHA-256 is checked before each launch and after the final actual JOIN;
a changed binary fails and prevents restart. Readiness must identify the
actual ephemeral loopback listener. No existing running core is accepted.

Defaults preserve the original 86,400× schedule speed: seven source days are
offered over seven wall-clock seconds. A host may be overloaded at that speed.
Full queues still count as offered inputs, cause a failed run and remain in the
ledger; the harness never reduces the denominator to improve delivery. Select
a slower schedule explicitly for a separate functional run:

```sh
python -m benchmarks.ori_bench.endurance \
  --candidate /absolute/path/to/immutable/ori-core \
  --output-dir /tmp/ori-sampled-week-slower-001 \
  --run-id sampled-week-slower-001 --speed 8640
```

This still offers exactly the same source-time shape and number of closed
samples over 70 wall-clock seconds. It does not establish a throughput target.
`--samples 3 --points 61` is a bounded smoke test with three hourly samples;
its report cannot claim the complete seven-day sampled workload.

## Original data and restart proof

The archive is fresh and replay-only. The harness requires persistent typed
batch/audio ingestion, empty initial session/project inventories and no active
workers. Each public acknowledgement and offered outcome uses the original
producer ledger. Verification checks every raw sweep/observation by exact ID,
public archive totals and the original source hash, retaining integer Hz,
UTC microseconds, raw units and float64 bits including signed zero and adjacent
representable values. The existing WAV verifier checks artifact identities,
metadata, closed bytes, frame counts and full source/retained PCM hashes.

When advertised, the unchanged original streaming NDJSON verifier also runs.
An absent export capability is explicitly `unavailable`, never an invented
successful export or a skipped positive check. The independent ingest/raw-ID/
WAV/restart proof still runs so incomplete candidates produce useful evidence.
The **overall** result remains `unavailable` and unsuccessful until that required
export check is available and passes.

The draft Guard is created before source production and its real status must
be inactive with an idle receiver. Its actual read-only preflight result is
retained before ingestion without asserting hardware readiness. Later epochs
check inactive status and exact project data; they do not turn the hardware
preflight estimator into a bulk historical scan.
Configured idle worker rows are retained as observations; every worker must be
`alive: false`, with no measurements, completed commands or start receipt.
Session start changes metadata only. No worker-start or guard-arm route is used.

A private SQLite evidence ledger stores all original event sequences and
payload digests. Sequence gaps, duplicates, missing events or changed payloads
fail. Exact operation counts are one `session.created`, one `session.started`,
and one received event for each offered sweep, observation and WAV: **506 events
for the complete workload**. Each restart must retain all those events and their
original payloads without appending duplicates. Current startup recovery resets
runtime stream states to `disconnected` while leaving session metadata running;
the report names that explicit runtime transition. IDs, settings, metadata,
timestamps and all other session fields remain exact. No recovery event or
resume field is fabricated. The second restart must be idempotent.

The source digest, complete WAV/PCM digest, event digest, inactive project and
bounded public history are checked again after each restart. Observed service
downtime is reported as a process lifecycle interruption, not RF measurement
coverage. No acquisition takes place during these checks.

## Ownership, bounds and outcomes

The existing POSIX `tests.release_process_ownership.OwnedCommand` owns the real
child tree and held process-session authority. A small trusted CLI adapter
supplies Core's stdin lifeline and closes it on an atomically published private
stop request. The adapter waits for the real child, then the original owner
waits and releases only after empty-members proof. The archive is reopened only
after successful graceful closure and JOIN. Failure or unknown cleanup retains
evidence and prevents another restart. No process is selected or signalled from
HTTP-returned PIDs. Unsupported process backends return explicit unavailable;
Windows process ownership is not claimed by this POSIX harness.

The original fixed producer queue, streamed WAV checks, one raw source at a
time, 100 summary rows, 16 Guard history rows and eight event rows bound public
working tails. The report includes observed maxima as well as limits. The
event/offer ledgers are disposable evidence, not canonical writers. These checks
do **not** establish Core internal registry memory, hard RSS, filesystem quotas,
physical I/O limits, continuous audio or hardware acceptance.

`report.json` records the immutable candidate SHA, source workload, actual
capabilities/preflight, offered/acknowledged/verified counts, all restart proofs,
owned child closures and explicit limitations. `producer/report.json`,
`producer/offers.sqlite`, `events.sqlite`, node logs and the canonical `data/`
remain available for inspection. No failure archive is automatically deleted.

Exit codes are 0 for the completed defined gate, 2 for unavailable candidate,
required routes or invalid command configuration, and 1 for failed preservation,
delivery or owned cleanup. A
report may contain `sampled_lifecycle_proof: "passed"` while its overall result
is unavailable because NDJSON is absent. That is partial functional evidence;
`successful` remains false. Missing binaries are never treated as successful
tests or replaced with a fake server.

Run regressions with:

```sh
python -m unittest discover -s benchmarks/tests -p 'test_*.py'
```

The tests exercise proof mutations, missing/incompatible executables, exact
denominators, output preservation and atomic private shutdown publication.
Real candidate outcomes belong to their separate retained reports, including
failed overloads. All validation described here is synthetic software work.

During development, the retained `b39b9a5` candidate delivered and verified the
complete workload at an explicitly slower schedule, but a subsequent Guard
preflight over 128 full 4,096-point sweeps exceeded the ordinary HTTP deadline.
That post-load responsiveness observation remains unresolved and its failed
report/archive is retained separately. Moving the hardware-preflight observation
before ingestion does not fix or pass post-load preflight responsiveness. The
sampled source/restart proof and that candidate performance issue are separate
results.

The separate bounded-page Linux run used that same immutable candidate, the
complete 168-sample / 4,096-point shape and explicit 8,640× speed. Every source
and all 80,640,000 PCM frames were verified before and after two restarts; all
506 original event sequences and payload digests remained exact. All three
owned Core epochs exited zero, closed gracefully and joined. Observed public
tails were 100 summary rows, 16 Guard history rows and eight events. The
independent sampled lifecycle proof passed; the overall result remained
unavailable because the candidate did not advertise NDJSON export. No missing
route, post-load preflight result or physical acceptance was promoted to pass.
