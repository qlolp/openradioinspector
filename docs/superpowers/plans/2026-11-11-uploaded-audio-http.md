# Uploaded Audio HTTP Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. The parent explicitly selected native execution with no nested agents.

**Goal:** Implement the seven original uploaded-PCM recorder and streaming routes in the Rust candidate.

**Architecture:** A new private Core module keeps a bounded uploaded recorder and hub, revalidates the original AuthReceipt, and returns inert/revocable bounded-memory response bodies. One retained shutdown attempt joins producer/finalization work without waiting for backpressured socket bodies. Existing Core/router/native-audio glue belongs to the parent.

**Tech Stack:** Rust 1.99.1, existing Tokio/Axum/serde dependencies, Python 3.12 original recorder/HTTP oracle, existing owned loopback process helper.

**Spec:** [Uploaded audio HTTP design](../specs/2026-11-11-uploaded-audio-http-design.md).

## Global Constraints

- Commit only new `crates/ori-core/src/audio_http/**`, new tests/fixtures and docs.
- Parent-provided existing-file compile glue stays uncommitted in this branch.
- Async initialization, GET and POST use the frozen signatures and original AuthReceipt.
- Uploads use 48 kHz mono PCM16, five-second history and a 600-second total recording bound.
- Eight recorders, four subscribers per stream, 64 queued blocks, 64 MiB recording/history and 16 MiB live storage.
- Frames retain 16 MiB request bodies; controls retain 64 KiB; streams retain the 120-second idle bound.
- No new dependency/lock changes, native capture engine, automatic device I/O, or fake Tune gate.
- Compile only with the allocated single-job cache; coordinate exclusive cache ownership.
- Logical allocation limits are not RSS, latency, physical I/O or hardware acceptance.

## Review Focus

- Many one-sample uploads must not create unbounded recording metadata.
- A slow live client must lose counted delivery without rejecting valid recording input.
- A completed WAV held across a new recording must remain stable and accounted.
- Revocation/disconnect must release subscription capacity while no PCM arrives.
- A nonreading response body must not deadlock the parent's Core-before-socket shutdown order.

### Task 1: Original Python oracle and sample validation

**Files:** Create `crates/ori-core/tests/audio_http_oracle.py`; create `crates/ori-core/src/audio_http/pcm.rs` and its focused tests.

**Interfaces:** The oracle returns actual PCM hex, recorder status and error type/message for finite boundaries, nested samples, numeric conversions, invalid shapes and invalid values. `pcm::decode_samples(&Value) -> ApiResult<Vec<i16>>` performs bounded conversion and preserves the original API error classification; PCM output matches Python bytes exactly.

- [x] Capture original Python results without modifying producer source or responses.
- [x] Add Rust regression vectors for clipping, negative zero, adjacent values, booleans/numeric conversions, empty nested input and malformed shapes.
- [x] Preserve the actual immutable-baseline HTTP 501 RED and the first focused Rust result once the compiler slot is available.
- [x] Implement bounded conversion with exact original error classification; run all oracle vectors.

### Task 2: Bounded rolling recorder and WAV views

**Files:** Create `audio_http/budget.rs`, `audio_http/recorder.rs`, and recorder tests under `audio_http/tests/`.

**Interfaces:** `Budget` issues RAII byte reservations. `Recorder` exposes push/start/stop/status/completed operations with an injected timestamp string. Completed recordings expose a canonical WAV header and immutable PCM chunk views.

- [x] Add regression tests for exact five-second trimming, prefix/recorded split, duplicate/idle lifecycle errors and automatic stop preserving full live input.
- [x] Add one-sample upload and small-budget controls to pin metadata/allocation limits and failure-before-mutation.
- [x] Implement a fixed ring and at-most-64-KiB coalesced chunks using the shared 64 MiB budget.
- [x] Verify actual Python status/count/WAV bytes, including stable old completed views while a new recording starts.

### Task 3: Counted bounded live delivery

**Files:** Create `audio_http/hub.rs` and hub tests under `audio_http/tests/`.

**Interfaces:** `Hub` tracks weak subscriber registrations; each response owns its subscription lease and queue. Shared PCM packets consume the 16 MiB budget once regardless of fan-out. Status uses the original session/stream/subscriber/queue/drop fields.

- [x] Add four-subscriber/64-block FIFO/drop-oldest tests and session/stream isolation controls.
- [x] Add global budget pressure controls showing that valid uploads remain accepted and all delivery losses are counted.
- [x] Implement global oldest-queued eviction and counted delivery dropping when only in-flight packets pin capacity.
- [x] Verify disconnect, empty blocks and revoked idle subscriptions release leases without producer backpressure.

### Task 4: Frozen HTTP facade and owned shutdown

**Files:** Create `audio_http/mod.rs` and lifecycle tests under `audio_http/tests/`.

**Interfaces:** Async `initialize`, `get`, `post`; `handles`; `AudioHttp::begin_shutdown() -> CloneCloseHandle`; `CloneCloseHandle::completed().await -> crate::Result<()>`. Consume `Core::session`, `AuthReceipt::revalidate/revoked`, `http::envelope`, `utc_now` and the frozen OnceLock field only.

- [x] Add exact-route/payload/missing-session/foreign-stream/error-priority tests for all seven endpoints.
- [x] Implement lazy recorders, original receipt checks, bounded response/subscription leases and zero-copy WAV/PCM bodies.
- [x] Add a latched admitted mutation test proving close waits for actual finalization before completion.
- [x] Add held unpolled PCM/WAV body tests proving the close attempt completes without waiting for body Drop and no later poll resumes production.
- [x] Verify cloned/cancelled close waiters observe one retained attempt and no device/backend construction occurs by default.

### Task 5: Actual loopback compatibility and handoff

**Files:** Create `crates/ori-core/tests/test_audio_http.py`; finish docs. Parent alone changes existing module/router/shutdown/capability glue.

- [x] Run the actual original Python HTTP producer and the actual selected Core binary through owned fixtures.
- [x] Compare all seven endpoints' statuses, original errors, counts, PCM bytes and complete WAV bytes without substituting HTTP data.
- [x] Exercise real disconnect, slow/nonreading PCM and WAV clients, counted loss and shutdown while mutation is latched.
- [x] Run focused module tests and the available complete Core/audio/network checks with one compiler job and the allocated cache.
- [x] Verify default zero native I/O, source/fixture integrity, all owned process closure and unchanged original budgets.
- [x] Commit only the new module/tests/docs and report exact software coverage plus unrun hardware/hosted-platform limits.
