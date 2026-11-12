# Acquisition pressure for derived work

Core's acquisition-pressure observer protects live analyzer capture by using the same `BackgroundQos` registrations as existing derived work. It does not change raw measurements, detector thresholds, archive contents, RF settings or device scheduling. Its timing thresholds are engineering policy, not measured hardware performance or a physical I/O guarantee.

## Design and scope

The chosen approach is one owned, bounded timer loop that reads cached per-worker timing. Deriving pressure from Guard arming would conflate authorization with reception. Querying SQLite or inspecting per-point data would add work to the acquisition path and miss an in-flight stall. Neither alternative is used.

`DeviceManager::acquisition_health()` copies at most the existing bounded registry's analyzer workers, then reads each worker's small timing lock. It performs no SQL, port work or native-state JSON serialization. Timing uses `std::time::Instant`, independently of archived UTC timestamps. A worker records poll start/completion and raw append acknowledgement once per capture; only a successful raw commit increments the completed-cycle counter. Receiver latency is excluded. Intentional instrument pause, Lab mode, stopping and absent acquisition do not block research.

The policy learns natural poll-plus-commit service time from successful cycles. The configured interval is an intentional sleep after capture and commit, so it is included only when checking whether the next capture is overdue. Continuous work with a zero configured interval is not itself a pressure signal. Confirmed range/RBW changes can occur within the same worker. The health cache therefore includes the existing opaque confirmed control revision and the monotonic time it changed after native acknowledgements. Changing this revision resets calibration and anchors a new initial grace; an ordinary status read or a slow sample does not. The revision covers broader confirmed control changes too, rather than pretending to be a separate scan-only configuration counter.

Startup defaults are a 250 ms observer interval, 90 s initial grace (matching the existing native scan deadline), 2 s minimum lag budget and 500 ms additional slack. Three independently observed successful cycles establish an initial baseline; subsequent healthy cycles adjust it gradually. After calibration, the lag budget is the larger of the floor and three times the learned service time plus slack. Two consecutive lag observations assert pressure. A pressured analyzer must provide three new healthy commits and at least one second of recovery before clearing it. Repeated reads of one cached healthy result do not count as recovery. Worker replacement and intentional pause reset their own calibration, without carrying an old instance's evidence forward. Options are trusted startup configuration, not HTTP inputs.

Timing is validated before Archive/writer initialization: the observer interval must be 10 ms–5 s; lag floor, initial grace and recovery dwell are bounded by 600 s, with grace at least the floor and floor/dwell at least the interval. Slack is at most 60 s. Invalid values, including overflowing durations and zero cadence, fail startup before canonical archive creation. Shadow mode starts no observer.

These defaults have not been tuned or accepted on a Raspberry Pi or physical analyzer. A persistent cadence change without a confirmed settings revision remains pressure evidence; it is not silently learned as the new normal. Explicit worker replacement or a confirmed control revision establishes a new calibration epoch. This observer does not issue either action itself.

Asserting pressure cancels existing registered derivative work through its original cancellation tokens and waits for its owned guard to be released after cleanup and JOIN. It blocks new registrations. Recovery permits a fresh retry. `ResearchPool` does not queue heavy jobs; this slice does not automatically restart or silently resume cancelled science. Existing retryable `acquisition_pressure` errors remain explicit.

The observer writes QoS only when its own policy changes. An idle observer has no pressure assertion to clear, so it preserves independent pressure requests while no analyzer is capturing. Repeated normal samples do not write `pressure(false)`.

The observer has an owned stop/JOIN completion that survives dropping an observer's close waiter. Core must stop and JOIN this loop before independently starting final Background shutdown, so recovery cannot clear shutdown pressure. Acquisition shutdown and derivative cleanup retain their existing owners.

## Verification

Fifteen focused regressions exercised the real Core/DeviceManager/instrument actors and owned temporary SQLite archives, plus deterministic monotonic policy cases. Before implementation, ten behavioral failures and five passing controls were observed; all fifteen then passed. The actual same-worker RBW operation changes synthetic scan duration only after its native command acknowledgement, proving confirmed-epoch recalibration without treating overload as a new epoch. A latched analyzer cancels the original research preparation grant, retains the heavy slot until its real thread JOIN, denies admission even after that slot is free, and permits a fresh retry only after recovery. Other cases cover an independently blocked receiver, no capture, independent explicit pressure, intentional pause, long natural scans, commit stalls, dropped close waiters and raw integer Hz/f64/unit preservation.

The combined Core library suite passed 69 tests with no failures or ignored cases, including existing Automation, original-credential lifetime, Control science, RAW, TLS, owned-worker and acquisition-health regressions. Commands used the existing warm target, one compiler job and one Rust test thread:

```sh
cargo test --locked --offline -j1 -p ori-core --all-features --lib
cargo clippy --locked --offline -j1 -p ori-core --all-targets --all-features -- -D warnings
cargo fmt --all -- --check
```

Static checks typecheck all Core targets; the runtime result above is the complete library suite. Final assembled-candidate integration/workspace validation is separate. Synthetic byte-stream and temporary-archive checks provide no physical RF, Pi resource, continuous-endurance or FPS acceptance.
