# Legacy per-session automation facade

`ori-automation` preserves the opt-in Python `automatic_detection`,
`automatic_handoff`, and `automatic_completion` policies separately from
Project Guard. Core owns the HTTP routes, archive, recovery, workers, scientific
processes and all device effects. This crate opens no ports or devices and does
not acquire a receiver's manual or Project Guard lease.

## Integration and original authorization

Construct `AutomationService::new(archive.clone(), sessions.clone(), effects,
config)` inside the core's Tokio executor. The supplied `SessionService` must
use that same `Archive` and domain coordinator. `configure(kind, session_id,
patch)` and `status` return the legacy policy/runtime JSON shapes. `start`
enables the supervisor loop; by default, an explicitly enabled policy starts
it automatically. `run_once` is the deterministic one-pass interface used by
fixtures, and has the same policy enablement and authorization checks.

Core implements `AutomationEffects`. Its synchronous `capture(AutomationScope)`
captures the **original** cached lifecycle, worker/tune and Guard generations
once at dispatch admission, before task/callback awaits. It returns a
`TuneCompletionCheck`; it must not do I/O or obtain a replacement permit.
`detect(DetectionRequest, original, cancellation)` returns typed detected
regions from the established Python algorithm via the same global
`ResearchPool` as other science/history jobs. `tune(TuneRequest, original,
cancellation)` performs the authorized receiver action and returns its original
`TuneReceipt`. Core must validate the provided original receipt before external
effects and honor cancellation while owning/reaping its work.

The facade composes this original Core receipt with its original policy epoch
and cancellation state. The additive Session hooks
`create_candidate_with_check` and
`start_eligible_investigation_with_effects` check cached admission while holding
the shared archive domain coordinator immediately before mutation. Handoff
rechecks the current highest-priority eligible queued investigation, including
the receiver filter and runtime-busy receivers. The post-tune receipt checks
both original admission and the returned worker receipt. Completion uses the
existing `complete_investigation_with_check`, which re-reads matching evidence
under the same coordinator. No delayed callback captures a fresh receipt.

Policy changes take the same coordinator for the epoch swap and audit commit.
Core stop, manual capture, reconnect and Project claim transitions must use its
existing coordinator/generation rules, so they invalidate the original checks.
The coordinator is never held across scientific work or receiver tuning.
Existing unchecked/manual Session methods retain their original validation and
behavior; this facade uses the additive checked entry points only.

## Policy compatibility and restart

All three policies are **runtime-only** and initially disabled. Audit events
persist as ordinary archive history, while policy enablement, processing
checkpoints and cooldowns do not. A restart requires explicit operator opt-in;
startup does not replay historical sweeps or investigations. Core's existing
recovery remains authoritative and historical results are never reset.

Detection defaults to a fixed raw-dBm threshold of -70, 100 kHz deduplication,
60-second cooldown and ten regions per sweep. It accepts only analyzer streams
with original `dBm`; it does not convert S-meter values or apply adaptive Guard
thresholds. Enabling/re-enabling seeds the current sweep ingestion boundary,
then processes pages of at most 1,000 new sweeps in ingestion order. Protected
candidate states and recent cooldown records suppress nearby duplicates using
the original bandwidth rules. It creates candidates and never enqueues jobs.

Handoff defaults to priority zero, all receivers, three consecutive failures
and 30-second retry delay. Queue/operator races are skipped rather than charged
to the circuit breaker. Receiver work stays independent of analyzer ingestion
and other policy/session dispatch slots.

Completion defaults to one matching observation and no listening delay. When
configured, its delay starts at `listening_started_at`, after tuning completes;
legacy records without that origin cannot satisfy a positive delay. Evidence
uses the exact target stream, frequency, normalized requested mode and time
at/after `started_at`. An AM observation cannot complete an FM investigation.
It never automatically fails or cancels an investigation.

IDs retain their original UTF-8 bytes. No Unicode normalization is applied to
maps, comparisons or queue ordering. UTC microseconds, integer Hz, original
units, float64 values and source metadata remain in the archive unchanged.

## Bounds and shutdown

The supervisor mailbox holds 16 commands, with one independent active task per
policy/session and at most 16 active tasks by default. At most 64 configured
sessions and 5,000 recent dedup records per detector are retained by default;
configured bounds are validated. Full task/checkpoint capacity returns an
explicit busy/capacity error and never silently discards source history.
Archive selectors retain their existing deadline and response-byte budgets.
Callback outputs are bounded by the configured maximum regions and validated
against original sweep point frequency/peak bits before candidate creation.

Disable/re-enable changes the epoch and cancels the previous task. A stalled
callback cannot block configuration or another dispatch slot. The supervisor
owns all task handles until actual join; dropping a request waiter does not
detach work. `close().await` stops admission, invalidates/cancels work and waits
for callback cleanup. Cancelling that close waiter does not cancel draining.
Dropping the last client also requests draining. Core must await close before
closing the archive, releasing its writer lease or stopping the executor.
Client disconnect alone does not close this service or disarm monitoring.

## Verification

`contracts/fixtures/automation-generate.py` runs the actual Python policies and
SQLite SessionService in owned temporary archives, with deterministic synthetic
clock/ID/tune callbacks and no background threads, network or physical devices.
Its committed oracle records the authority/generator source hashes, 97 status,
event, validation and combined-policy actions, and original Python scientific
region vectors. The Rust fixture keeps real archive and Session logic and
substitutes only scientific/device callbacks. Additional tests exercise blocked
science/tune, epoch and original-receipt invalidation, coordinator admission,
priority/receiver races and cancellation-safe shutdown.

After root dependency/lock integration, the focused checks are:

```sh
cargo test -p ori-automation -p ori-session --locked -j 1 -- --test-threads=1
cargo clippy -p ori-automation -p ori-session --all-targets --locked -j 1 -- -D warnings
cargo fmt --all --check
```

Development uses an external Cargo harness pointing at the actual sources and
tests, keeping tracked root manifests/lockfiles unchanged. These are synthetic
software checks. Physical receiver behavior, Pi throughput and long-duration
resource/latency acceptance remain unrun; the Rust candidate is not silently
promoted over the Python default.
