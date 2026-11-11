# Modernization delivery plan

Accepted specification: the user's OpenRadioInspector modernization plan of
2026-11-11. This document records the delivery gates; implementation status and
measured results are reported separately. Targets are not measured claims.

## Target

An offline Raspberry Pi 5 / 8 GiB / SSD node with tinySA Ultra and AR-DV1,
shared Web/Windows UI and native macOS UI. Preserve both workspaces, raw data,
IDs, integer-Hz frequencies, UTC precision, archive ordering, public APIs and
CLI compatibility. Only one active core owns hardware and archive writes.

* Core: Rust edition 2024, Tokio, Axum; Python 3.15 / NumPy 2.5 research worker.
* Web/Windows: TypeScript 7, React 19.3, Vite, Tauri 2.
* macOS: Swift 6.4, SwiftUI, Metal, macOS 14 deployment target.
* Canonical archive: SQLite 3.54.1 WAL/FULL, one serialized writer.
* Derived analytics: DuckDB 2 LTS / Parquet snapshots, never canonical writes.

Pin tested patches and toolchains. Preserve the current Python environment
through the transition. No automatic RF generation or raw-data deletion.

## Delivery sequence

1. Capture reproducible synthetic baselines, traces and migration contracts;
   fix Windows HTTP fixture races; lock dependencies and record runtime versions.
2. Strengthen durability, bounded archive queries and verified backups/restore;
   publish OpenAPI and common contract fixtures for the three SDKs.
3. Deliver shared React Web/Windows UI over existing REST/SSE, preserving a
   legacy fallback. Refactor native macOS screens and move work off the UI thread.
4. Build the Rust candidate by subsystem; compare with Python on replay and
   temporary archives. Shadow mode cannot open hardware or mutate live archives.
5. Deliver bounded history/viewport queries, snapshot analytics and negotiated
   Protobuf/WebSocket streaming, with sequence-gap recovery.
6. Add exclusive cutover/rollback and offline service packaging, pairing/TLS.
   Preserve shutdown versus disarm intent, preflight, opt-in autostart and
   latest committed data; mark the coverage gap and interrupted investigations.
7. Run the complete synthetic, installation/rollback and physical acceptance
   gates. Rust becomes the default only after acceptance. Retire the old server
   and Tk UI after two accepted stable releases, preserving data/API compatibility.

Each step is delivered as small reviewable PRs with tests and documentation.
Hardware acceptance cannot be replaced by mocks, replay or accelerated clocks.

## Resource and responsiveness gates

Reference: Pi 5 / 8 GiB / SSD, 1080p, three clients on gigabit LAN, including
research, export and backup in the background.

* p95 input reaction <=100 ms, queued-command acknowledgement <=200 ms,
  committed-sweep-to-render <=250 ms; interactive rendering >=30 fps.
* Warm first 100 history summaries <=500 ms at 1,000,000 x 290-point sweeps;
  first useful screen <=3 s, excluding hardware preparation.
* Rust core RSS <=256 MiB; background app processes <=1.5 GiB; local UI <=1 GiB.
* One heavy analytics job; <=2 DuckDB threads / 512 MiB; worker RSS <=768 MiB;
  derived cache and temporary storage each <=10 GiB; background I/O <=20 MiB/s.
* At the 30-minute target load, archive all 18,000 offered 4096-point sweeps,
  18,000 receiver observations and all 48 kHz mono PCM. Count offered inputs,
  not only accepted inputs. Test deliberate overload separately, with visible
  gaps/rejection and bounded queues.

## Verification

Retain full existing CI while adding Rust/TypeScript/Python 3.15 gates. Run
release checks with diagnostic artifacts, numerical/command parity, migration
and restore tests, seven-day accelerated lifecycle scenarios, USB/audio/webhook
and disk-full faults. Test UI tasks with new users. Physical release acceptance
requires an eight-hour two-instrument run and an autonomous-node endurance run.
