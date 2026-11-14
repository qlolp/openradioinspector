# Shared v1 contract and SDK foundation

The authoritative schema is `radioinspector.web.openapi.document()`. The
version-controlled Python generator (stdlib plus the pinned Rust formatter) emits OpenAPI 3.1 JSON, a schema
fingerprint, typed domain models and operation/path metadata for Rust, TypeScript
7 and Swift 6.4. Existing clients can adopt these through adapters; this does not
replace their transports or change `/api/v1/*`.

## Generate and check

Run from the repository root, with the Python core dependencies installed:

```sh
python tools/contracts/generate.py
python tools/contracts/generate.py --check
```

`--check` compares bytes in memory and exits 1 for missing/stale outputs without
rewriting anything. Both object key order and whitespace in an input JSON capture
are irrelevant. For an isolated worktree, use
`--schema-source /path/to/radioinspector/web/openapi.py`; an exported OpenAPI JSON
file is also accepted. The Python module is still the authority after integration.
The pinned Rust 1.99.1 `rustfmt` must be on PATH or in `CARGO_HOME/bin`;
`RUSTFMT` can point to its executable. No network or hardware is involved in generation.

Change runtime helpers in `tools/contracts/runtime/`, then regenerate. Generated
models and registries carry a do-not-edit header. The generator format and schema
SHA-256 are recorded in `contracts/generated/manifest.json`. Generated dependency
manifests pin TypeScript 7.0.2; npm and Cargo lock files retain package integrity
hashes. Toolchain patches used here are Rust 1.99.1 / edition 2024, Node 26.13.0,
Swift 6.4.2, and Python 3.15.1 (the generator also runs on the existing Python
3.12.14 core environment). The generator itself has no Python package dependencies.
Schema validation tests use the core's pinned jsonschema 4.26.0.

## Wire behavior and consumption

- Rust: depend on `crates/ori-contracts`; models are reexported at `ori_contracts::*`.
  `from_json_slice::<Sweep>(bytes, max_bytes)` rejects duplicate keys at every
  depth, invalid UTF-8, nonfinite/overflow numbers and oversized bodies before
  model deserialization. `to_json_vec(&value)` validates nested floats before
  serde_json can turn NaN/Infinity into null. The generated `Sweep` and
  `ReceiverObservation` use `u64` Hz and `f64` levels, not float32. Metadata and
  flattened unknown fields retain serde_json values with `arbitrary_precision`
  and `float_roundtrip`; arbitrary-size integer metadata never passes through
  binary64. A reversible collision-free key escape protects real metadata whose
  names match serde_json's internal numeric protocol; original keys are restored
  after model decoding. `from_json_slice` requires `RestoreKeys`, which generated
  models, Value, Option, Vec and JSON maps implement. Custom DTOs restore their
  embedded JSON fields through this trait. Use `parse_json_value(bytes,max_bytes)`
  for archive/raw JSON reads; it builds values directly without ValueVisitor. `UtcTimestamp` supports `FromStr`, `TryFrom<&str>`,
  `as_str()` and integer `unix_micros()`.
- TypeScript: import `contracts/generated/typescript/index.ts` from the Web/Tauri
  adapter. Use `parseJSON(textOrBytes, maxBytes)`, then `decodeSweep(value)` or the
  other generated model decoder. Encode known models with `encodeSweep(model)`.
  Integer fields use `Integer = number | bigint`: tokens beyond JavaScript's safe
  integer range become bigint without rounding. `stringifyJSON` preserves `-0`
  and refuses unsafe integer-valued JS numbers; explicitly use bigint for large
  integers or `float64(value)` for intended integral-valued doubles. Parsed large
  float tokens retain that distinction through `Float64`. Generated model encoders
  already mark known double fields, including the full finite binary64 range.
- Swift: add the local package at `contracts/generated/swift` and import
  `ORIContracts` (macOS 14 minimum). Use `StrictJSON.decode(Sweep.self, from:data,
  maxBytes:limit)` and `StrictJSON.encode(model)`. Typed models also expose
  `init(json:)`, member initializers and `json`. `JSONValue.integer(String)` retains
  arbitrary-size integer metadata tokens; known Hz fields are UInt64. `JSONObject`
  compares UTF-8 key bytes so composed/decomposed Unicode keys remain distinct.
  Codable conformance is a compatibility adapter for existing clients;
  Foundation's generic JSONDecoder cannot guarantee strict duplicate-key handling
  or retain arbitrary integer tokens. StrictJSON is the wire entry point.

All three retain unknown model fields and nested metadata, exact ID UTF-8 bytes,
original level units, negative zero and adjacent binary64 values. Optional null
model fields can be omitted when reencoded. No S-meter calibration or physical
frequency bounds are invented. Time normalization preserves six microsecond
digits and converts offsets to UTC using integer arithmetic. Legacy naive ISO
times assume UTC; a space separator, absent fraction and excess fraction digits
(truncated to microseconds) remain supported. Stored archive rows are not rewritten.

The operation registries retain method, v1 path, query names and documented body
budgets. `encode_path_segment` (Rust), `encodePathSegment` (TS/Swift), and TS/Swift
`operationPath` encode a raw ID exactly once as one path segment. Reserved bytes
such as `/`, `+`, `%`, `?` and non-ASCII UTF-8 cannot alter routing. Transport,
reconnect/authentication, endpoint-specific request limits and core semantic
validation remain the existing clients/server's responsibility.

The default TS/Swift codec size guard is 16 MiB and nesting is limited to 128;
callers pass the endpoint's actual byte limit. Rust callers always pass a byte
limit; the strict parser bounds nesting to 128. OpenAPI intentionally leaves advanced
scientific/control payloads open. These libraries provide typed known fields and
strict JSON transport, not complete semantic validation of every legacy function.

## Hardware-free checks

Keep build caches outside the checkout and cap compiler concurrency at two:

```sh
python -m unittest discover -s contracts/tests -p 'test_*.py' -v
CARGO_TARGET_DIR=/tmp/ori-contracts-target cargo +1.99.1 test --locked --manifest-path crates/ori-contracts/Cargo.toml -j 2
npm ci --prefix contracts/generated/typescript --ignore-scripts --no-audit --no-fund
npm --prefix contracts/generated/typescript run check
node --test contracts/tests/typescript.test.ts
swift test --package-path contracts/tests/swift-goldens --scratch-path /tmp/ori-contracts-swift -j 2
python tools/contracts/generate.py --check
```

Rust, TypeScript and Swift independently consume the same checked-in
`contracts/fixtures/measurement-v1.json` and assert literal float64 bit patterns,
integer Hz, UTC microseconds, IDs and original S-meter units. Additional cases
exercise unknown metadata, large integers, full finite float range, byte-distinct
Unicode keys, invalid UTF-8/JSON, nested/escaped duplicate keys, nonfinite numbers,
exact size boundaries and legacy timestamps. The generator test deliberately
stales an artifact and checks non-mutating failure, then regenerates deterministically.

These are synthetic codec/compiler checks. Physical hardware acceptance,
full client integration and performance acceptance are separate release gates.
