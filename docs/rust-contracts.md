# Shared baseline and runtime contracts

`radioinspector.web.openapi.document()` remains the authority for the Python
baseline: 117 paths, 135 operation IDs and the existing schemas and codecs.
`contracts/runtime-extension-v1.json` is the reviewed additive source for the
Rust candidate. The generator composes it into
`contracts/generated/runtime-openapi-v1.json` (130 paths, 149 operations).
The later Core delivery slice will consume
`ori_contracts::RUNTIME_OPENAPI_V1_JSON`; it must not maintain a
second handcrafted OpenAPI document or derive the contract from a running node.
The companion runtime manifest records the baseline, extension and runtime
document SHA-256 fingerprints.

The runtime document retains baseline paths, operation IDs and schemas. Every
operation has explicit `x-ori-backends`, feature requirements, capability
requirements and runtime support status. An inherited baseline operation is a
`compatibility-target` until the supplement maps its candidate support. Its
presence in the catalog is not a claim that Core implements it. New security
and binary WebSocket operations list only `rust-candidate`; the unchanged
Python document does not advertise them. Support mappings are reviewed source,
not inferred from a mutable server's route list.

Feature requirements apply to the Rust candidate. For example, sweep summary
pages require `sweep_summaries`, archive export requires
`session_archive_export`, and task persistence requires `control_task_planning`.
Enabling the scheduler additionally requires `control_task_execution` when
the request contains `enabled: true`. Planning does not imply execution,
physical device readiness or acquisition acceptance. The binary WebSocket
requires the negotiated `ori.measurements.v1.protobuf` capability. A missing
feature/capability excludes the operation; an unknown backend is an error.
These descriptors guide client selection; the server still enforces policy.

In LAN mode, every API route requires a valid native Bearer or browser session
except `GET /api/v1/security/status` and `POST /api/v1/security/pair`. Pairing
still requires trusted TLS/Host/Origin and a locally opened single-use window.
Browser-session creation accepts valid Bearer credentials only. Cookie
mutations require Origin, and revocation invalidates native and browser
sessions. The document describes the explicit loopback mode separately;
internal authenticated runtime IPC is outside the public API catalog.

SDK supplements are separately importable:

- TypeScript: `contracts/generated/typescript/runtime.ts` exports runtime DTO
  decoders/encoders, `runtimeOperations`, and
  `selectRuntimeOperations(backend, features, capabilities, request)`.
- Swift: the existing ORIContracts target includes `Runtime.swift`, with the
  same DTOs, descriptors, and `selectRuntimeOperations(backend:features:capabilities:request:)`.
- Rust: `ori_contracts::runtime` exports DTOs, `OPERATIONS`, and
  `select_operations(backend, features, capabilities, request)`.

Descriptors include the method, canonical path parameters, query names, request
byte limit and request-dependent feature conditions. Pass feature names whose
server values are true, and capabilities actually negotiated from the node.
Use the baseline path-segment helpers for IDs; IDs never become filesystem
paths. `SecurityStatus`, browser/native pairing results and the measurement
WebSocket descriptor add only existing wire fields. Summary pages reuse the
baseline `SweepSummaryPage` type. The transport codec implementations remain
unchanged: exact unsigned integers, finite float64 including negative zero,
private-looking JSON names and distinct NFC/NFD keys remain intact. Unsupported
WebSocket versions are rejected by all three runtime decoders; existing client
SSE fallback behavior is independent of this supplement.

From the repository root, using the pinned toolchains:

The Python checks use `jsonschema==4.26.0`; importing the existing authoritative
Python module also requires its NumPy dependency (`2.5.4` in the checked profile).

```sh
python tools/contracts/generate.py --check
python -m unittest discover -s contracts/tests -p 'test_*.py'
node --test --test-reporter=tap contracts/tests/typescript.test.ts
npm --prefix contracts/generated/typescript run check
CARGO_BUILD_JOBS=1 cargo test -p ori-contracts --locked --offline
CARGO_BUILD_JOBS=1 cargo clippy -p ori-contracts --all-features --all-targets --locked --offline -- -D warnings
swift test --package-path contracts/tests/swift-goldens -j 1
```

The default generator checks both variants. `--variant baseline` selects the
unchanged baseline output set; `--variant rust-candidate` selects additive
runtime outputs only. Unknown extension names, versions, backends or policy
keys, component replacement, operation ID/path collisions, unresolved schema
references and unresolved path parameters fail before outputs are written.
Selecting the legacy variant still validates the supplied extension.

The owned runtime probe and its process helpers arrive in the final verification
delivery slice. Once those tools are present,
`tools/contracts/probe_runtime.py --core-binary IMMUTABLE_BINARY --report-dir
FRESH_DIRECTORY` starts one owned replay node on ephemeral loopback, uses only
new synthetic measurements, and joins it before completion. It retains raw
responses, the new archive, process log and a binary fingerprint even after
failure. `--require-shared-openapi` additionally requires exact equality with
the generated runtime document, so an older handcrafted Core document fails
that check. No test silently skips a missing compiler, binary or dependency.

Verified on 2026-11-11: Python 3.15.1 passed 13 contract tests and four existing
OpenAPI behavior tests; Rust 1.99.1 passed 13 SDK tests; Node 26.13.0 passed 11
SDK tests and TypeScript 7.0.2 strict checking; Swift 6.4.2 passed all 12 SDK
tests. No tests were skipped. The generator checked all 20 outputs, strict
Clippy and workspace formatting passed, and 44 existing generated/fixture/codec/
model/operation files were byte-identical to the starting revision. All three
unknown-WebSocket-version regressions and the explicit-invalid-extension CLI
regression failed before their fixes; their failure data was retained outside
the repository.

The immutable `5c7ebf0` probe verified actual security status, loopback pairing
refusal, binary descriptor fields, task execution refusal, one-sweep summary
schema, exact float64 negative zero, explicit null cursor and graceful owned
shutdown. Positive pairing DTO fixtures use a deliberately invalid synthetic
token. This probe performs no LAN credential provisioning, hardware/serial
access, performance or physical-storage acceptance. The same binary fails the
strict shared-document check until the HTTP owner embeds the new constant.
