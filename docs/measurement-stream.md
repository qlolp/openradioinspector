# Measurement stream v1 codec

This is a shared, bounded codec library. It does not open sockets or devices,
write archives, advertise an enabled transport, or implement replay/reconnect.
The existing REST/SSE JSON envelope remains the compatibility format. Core owns
WebSocket negotiation, authentication, TLS/Origin policy, cancellation and
recovery from the persisted event sequence.

The canonical IDL is `contracts/protobuf/measurement_stream.proto`. Rust uses
checked-in Prost derives rather than a build-time `protoc` executable; Prost is
exactly pinned to [0.14.5](https://docs.rs/prost/0.14.5/prost/). Field numbers,
wire bytes, semantic metadata and float bits are checked against an independent
Python stdlib producer, shared with the TypeScript and Swift decoders.

## Encoding and preservation

Each frame carries protocol version 1, a positive unsigned 64-bit sequence and
the standard `api_payload(event)` envelope. The sequence must exactly match the
integer JSON token at `data.sequence`. Unknown event kinds, properties, units,
IDs, Unicode key spellings, timestamp strings and metadata remain opaque.
JSON parsing uses the shared strict contract codec, including duplicate-name,
UTF-8, nonfinite-number and reserved-Serde-key protections.

Only `sweep.received` at `data.payload.sweep` may use `SpectrumArrays`. Both
`frequencies_hz` and `levels` are removed from its metadata, with all other
fields retained. Packed Hz are ordered nonnegative uint64 values; levels are
finite IEEE-754 binary64 values. Empty/unequal arrays and metadata that already
contains either removed array are rejected by typed decoders.

The encoder packs only integer frequency tokens in uint64 range and explicit
float level tokens. Integer-valued levels retain their original JSON numeric
kind through generic fallback. Frequencies larger than uint64 also remain full
JSON. Packing is chosen only when it fits and reduces the encoded frame size;
otherwise the full original JSON bytes are retained without spectrum data.
No source arrays are rounded or truncated. Typed reconstruction preserves each
double's bits, including negative zero and adjacent ULPs; JSON whitespace and
float spellings can change after a typed reconstruction.

## Interfaces and budgets

Rust `ori_stream::{encode_frame,decode_frame}` accept strict JSON bytes and wire
bytes respectively. `*_with_limits` accepts lowering-only `Limits`.
`DecodedFrame` contains `sequence`, parsed `envelope`, original `envelope_json`
metadata bytes and optional `SpectrumArrays` (`Vec<u64>`, `Vec<f64>`).
`into_envelope` inserts arrays directly into the parsed structure.
For a bridge forwarding original bytes, `validate_frame` (and its limits
variant) returns `FrameHeader {protocol_version,sequence,has_spectrum}` after
the same strict validation, without allocating the packed numeric vectors or
reencoding opaque JSON. Generic JSON still requires strict parsing.

TypeScript `decodeFrame` returns a bigint sequence, the parsed envelope, raw
`envelopeJSON` bytes, and optional `BigUint64Array`/`Float64Array`. Swift's
`MeasurementStream.decode` returns UInt64, `JSONValue`, raw `Data`, and optional
`[UInt64]`/`[Double]`. These APIs avoid point-array JSON stringify/reparse. Their
optional structured reconstruction helpers are for callers needing the full
JSON envelope; renderers can consume typed arrays directly. TypeScript uses the
SDK's `Float64` marker when reconstructing double JSON values. Across a worker
boundary transfer the raw metadata bytes and typed arrays; structured cloning
an arbitrary SDK `Float64` object loses its prototype.

The hard frame cap is 16 MiB + 64 KiB; a recognized sweep has at most 1,000,000
points in either encoding. Other metadata arrays are governed by frame/JSON
budgets. Limits cannot raise these caps. Readers bound varints and declared
lengths before allocations, reject duplicate unique protobuf markers and
unsafe wire types, and tolerate unknown fields with safe scalar wire types.
Repeated numeric fields may be packed, unpacked or split across valid segments.

`capability()` is library metadata for a future router. Its path is
`/api/v1/events/ws`, subprotocol `ori.measurements.v1.protobuf`, transport
`websocket`, encoding `protobuf`, version `1`, and `max_frame_bytes` as above.
Its presence does not establish that a server endpoint is enabled.

## Reproduction

Verified on Rust 1.99.1, Node 26.13.0 / TypeScript 7.0.2, and Swift 6.4.2 on
Linux: 8 Rust, 4 TypeScript and 3 Swift tests pass, plus Rust fmt/Clippy with
warnings denied and the generated TypeScript package typecheck. The portable
Swift package targets macOS 14; this verification does not claim a macOS UI run.
There are no skipped tests or physical-device checks in this codec slice.

The shared oracle has 15 valid and 42 malformed frames. It covers large
sequence/Hz integers, opaque Unicode/private keys, original units/times, first
tied peak order, negative zero/subnormal/ULP bits, packed/unpacked/split arrays,
unknown safe fields, duplicate markers/JSON names, UTF-8/BOM, depth, truncation,
overflow, wire types, nonfinite values, header mismatch and byte/point budgets.
Every incomplete prefix of the canonical 575-byte typed fixture is rejected.
SHA-256 of the IDL is
`dbb5a7c573e5e905155ea528382eaf8634d6fe213bf084f0738c15ccc180698e`;
the deterministic `stream-v1.json` fixture hash is
`d18011031dc11b03586c0357c4e25b34a8dbaf74d22c7f190664ec72b4a501c5`.

Regenerate only the owned synthetic fixtures with
`python contracts/protobuf/generate_stream_fixtures.py`. There is no system
Protobuf compiler or production dependency on Python fixture generation.

Focused checks (use the repository's pinned Rust/Node/Swift toolchains):

```sh
cargo test -p ori-stream --test wire -- --nocapture
cargo test -p ori-stream --features serde_json/preserve_order --test wire
cargo clippy -p ori-stream --all-targets -- -D warnings
node --test crates/ori-stream/tests/typescript/wire.test.ts
swift test --package-path contracts/protobuf/swift-stream -j 2
```

The regular 4096-point test checks size and lossless reconstruction. Run timing
separately on an idle host with `cargo run -p ori-stream --example diagnostic`
(an optional output directory writes the two synthetic files for the TypeScript
`crates/ori-stream/tests/typescript/benchmark.ts` script). These timings are a
local host diagnostic, not Raspberry Pi, WebSocket, rendering, hardware or
end-to-end latency acceptance evidence.
Timing was deliberately deferred during shared compilation and browser gates;
no timing result is claimed by this codec release.

Typed metadata uses recursively sorted object keys, including when another
workspace package enables `serde_json/preserve_order`. The assembled release
gate caught that feature combination changing encoded bytes after array
removal. Both feature configurations now share the unchanged independent
canonical-byte oracle. Generic frames retain their original JSON bytes.
