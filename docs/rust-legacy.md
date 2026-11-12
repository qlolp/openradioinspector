# Legacy panorama workspace

`ori-legacy` provides the existing panorama workspace as an independent Rust
library. Its scientific callback runs the original Python `ApplicationState`,
`Handler`, `PanoramaStore`, threshold, signal, coverage and measurement helpers.
There is no second implementation of their NumPy algorithms.

The library does not implement HTTP, SSE transport, device access or the global
runtime writer lease. Python remains the default application until Core wires
and accepts this compatibility surface. This work does not change the modern
archive/history service or convert legacy floating-point grids to integer Hz.

## Public interface and HTTP mapping

Construct `LegacyWorkspace::new(LegacyOptions, Arc<dyn LegacyScience>)` inside a
Tokio runtime. The default state has version 0 and no database. Creating the
workspace or reading this empty state never opens or creates a `.pan` file.
`LegacyOptions.library` is the optional canonical containment boundary;
`scratch_directory` is a trusted server-selected staging directory.

| Existing route | Library method / query | Plain legacy response |
| --- | --- | --- |
| GET `/api/state` | `snapshot(true)` / `LegacyQuery::State` | Full legacy snapshot |
| GET `/api/signals` | `signals()` | `{"signals":[...]}` |
| GET `/api/coverage` | `coverage(rows, cols)` | Existing GeoJSON or `{}` |
| GET `/api/occupancy` | `occupancy(threshold_db)` | Original occupancy result or `{}` |
| GET `/api/efficiency` | `efficiency(threshold_db)` | Original efficiency result or `{}` |
| GET `/api/keywords` | `keywords()` | Original catalog and count |
| GET `/api/measure` | `measurements(start_hz, stop_hz, norm_hz)` | Original measurement result or `{}` |
| POST `/api/threshold` | `threshold(Value)` | `{"ok":true,"threshold":...}` |
| POST `/api/database` | `load_database(&Path)` | `{"ok":true,"version":...}` |
| GET `/api/stream` | `subscribe()`, `version()`, `snapshot(false)` | Core sends legacy snapshot on change |

`query_json(LegacyQuery) -> Result<Arc<[u8]>>` returns cached or computed strict
JSON without copying point arrays through another `Value`. Prefer it for HTTP.
The convenience methods above return `Value`. Responses are plain legacy bodies;
the library does not wrap them in the modern API envelope. Core owns HTTP query
string conversion, missing-body/path errors, authentication, request-body limits,
the legacy SSE initial snapshot and heartbeat, and subscriber lifetime.

Successful load and threshold reconciliation each increment the public version
exactly once. An empty threshold object also reconciles and increments, as the
existing handler does. Queries and failures do not increment it. The watch channel
reports the latest version, rather than promising one queued notification per
change. A loaded `snapshot(false)` omits `waterfall`; the no-database response
retains its original `waterfall: []` field.

`LegacyError::{http_status,code,api_message,legacy_body}` supports Core's mapping.
The last method returns `{"ok":false,"error":message}`. Original scientific
validation errors keep their message and status. Library validation and SQLite
errors use 400; resource bounds use 413; changed sources use 409; busy, closed,
cancelled or unconfirmed cleanup use 503; invalid scientific replies and storage
failures use 502. These operational errors are explicit additions, not fabricated
scientific results. Coverage is 1..100 per dimension; optional numeric arguments
must be finite and measurement stop must exceed start.

## Ownership, publication and preservation

One bounded actor serializes each workspace. One dedicated SQLite thread owns
the selected file. An exclusive advisory file lock is acquired before SQLite
opens an existing file; another cooperating workspace receives `Busy`. Core must
also retain its global writer lease. Uncooperative external SQLite writers are
detected through `PRAGMA data_version`, with compare-and-set checked again under
`BEGIN IMMEDIATE` before a threshold commit. Changed files require explicit reload;
cached reads do not silently combine old working state with new raw records.
File identity is checked through Unix device/inode or Windows volume/file-index.

The source is opened without `CREATE`, schema initialization, migration or journal
mode changes. The existing four-table `.pan` schema is required. The canonical
write is one transaction updating only threshold line 0's two original f64 BLOBs.
Other threshold lines, panorama rows/BLOBs, metadata, signal events, IDs, units,
time strings and original schema remain untouched. A sibling `signals.json` is
copied for initial load and never rewritten. Its signal rows remain the working
rows until the actual threshold handler rebuilds them.

A successful scientific result is validated and bounded before any canonical
write or public state change. A failed load preserves the prior selected source
and state. A failed threshold preserves the prior threshold and version. If the
SQL commit wins a concurrent close, its committed state is published before
shutdown. No historical measurements or baselines are rewritten.

Two conservative refusals are deliberate differences from Python's permissive
`PanoramaStore` initialization: incomplete existing schemas are rejected rather
than initialized, and threshold writes are refused when custom triggers or
foreign keys referencing `threshold_line` could alter other rows. Reads remain
available for otherwise valid custom schemas. No constraints are removed.

`close()` is out of band: it stops admission, cancels the active token, rejects
queued requests, waits for the scientific callback to finish/reap, then closes
and joins SQLite and the actor. A timeout or failed JOIN returns
`CleanupPending`, retaining file ownership. Retrying close may finish pending
cleanup. A failed actor JOIN deliberately retains its lease until process exit;
dropping public handles does not claim cleanup succeeded.

## Scientific reservation and one-shot RPC

Core implements these object-safe traits with the **same** shared ResearchPool
reservation used for other scientific work:

```rust
trait LegacyScience: Send + Sync + 'static {
    fn try_reserve(&self) -> Result<Box<dyn LegacySciencePermit>>;
}
trait LegacySciencePermit: Send {
    fn execute(self: Box<Self>, request: Value,
               cancel: LegacyCancellation) -> ScienceFuture;
}
```

The noncloneable reservation is acquired before snapshot preparation, consumed by
the child invocation, and dropped on any preparation error. Do not add a second
independent heavy-job semaphore. Bridge `LegacyCancellation` to the runtime child
cancellation token and await child/IPC reaping before completing the future.
Dropping the callback future on cancellation would violate file ownership.

Production invokes an installed, trusted interpreter with
`-I -m radioinspector.legacy_rpc`. Set OMP, OpenBLAS, MKL, NumExpr, vecLib and
Arrow thread limits to 1 **before spawning**: package imports can precede the
module's own environment setup. The callback is fixed to this worker; HTTP
cannot choose a module, SQL, command, URL or snapshot path.

The request is one strict UTF-8 JSON object followed by EOF:

```json
{
  "version": 1,
  "request_id": "opaque bounded id",
  "operation": "analysis",
  "method": "legacy_workspace",
  "action": "load",
  "source_version": 0,
  "database": "/operator/selected/original.pan",
  "state": {"threshold": null, "signals": []},
  "parameters": {},
  "legacy_limits": {},
  "snapshot_directory": "/private/server/chosen/stage",
  "snapshot_nonce": "unpredictable seal"
}
```

Actions are exactly `load`, `state`, `signals`, `coverage`, `occupancy`,
`efficiency`, `measurements`, `keywords`, `threshold`. Parameters follow the table
above: `include_waterfall`, `rows/cols`, `threshold`, `start/stop/norm`, or the
original threshold object. Load/signals/keywords use `{}`. No-database/keyword
requests omit snapshot fields and set database to null. The optional `limits`
object recognizes only the existing ten lowering-only ResearchPool limits.
`legacy_limits` recognizes the six source/result limits below, also lowering-only.

The trusted supervisor may additionally inject the private top-level `shared_io`
capability specified in [the RPC contract](research-rpc.md#private-shared-file-io-capability).
It is validated before scientific dispatch or temporary-directory creation.
Successful capped requests add only the outer `shared_io_receipt` counters;
Core consumes that field before passing the original five-field envelope to
LegacyWorkspace. Plain legacy HTTP results and uncapped RPC responses retain
their original shape and bytes. No capability path/nonce is returned.

The worker's second SQLite copy reserves the actual logical source pages for
read plus write, in at most 64 KiB units, using the same ledger. Manifest reads
and optional sidecar copy reads/writes are bounded to 64 KiB. The unchanged
legacy loader's subsequent sidecar read is reserved by its exact byte length
in those same logical units before invoking that helper. The worker adds no
extra hash pass or local rate timer. Its receipt excludes interpreter/library
imports, RAM/pipe traffic and OS/SQLite internal cache, journal and syscall
behavior; it does not claim physical I/O or filesystem-quota enforcement.
The source is read-only; every SQLite connection closes before owned temporary
files are removed. A failed charge returns no success receipt. A capped
working directory is inside the supervisor-owned snapshot tree, so a parent
that terminates/reaps an interrupted child can clean the whole owned tree.

Source verification on 2026-11-11 used Python 3.15.1 with an explicit source
bootstrap: all 36 focused Legacy/research RPC tests passed, including all nine
capped Legacy actions and fourteen zero-file-I/O point methods. Tests compared
the ordinary frames, original source hashes, float64/Unicode data, exact ledger
increments and 64 KiB charge bounds; they also denied a charge during a real
SQLite copy and terminated/reaped an actually blocked child before cleanup.
The separate ordinary-script oracle passed 15 isolated requests using installed
scientific modules. This is distinct from validating the newly rebuilt
installed `-I -m` entry point with `--shared-io`; that command records module
paths/hashes and requires private success receipts before acceptance.

Success is `{version,request_id,operation:"analysis",ok:true,result:{reply,...}}`.
`state` is present in `result` only for load/threshold and is a complete legacy
snapshot; `reply` is the plain HTTP body. Errors use the existing research error
envelope including `http_status` and `api_message`. Identity, version, field shape,
threshold dimensions, finite f64s and increasing grids are checked before publish.
Strict codecs preserve large IDs, negative-zero float tokens, Unicode distinctions
and f64 precision. Original `jsonio` behavior still maps nonfinite scientific
output to JSON null. Raw stored measurement BLOBs are never normalized.

The library uses SQLite backup in 128-page chunks from one read transaction,
copying at a cooperative default 20 MiB/s combined read/write budget and checking
cancellation/deadline. The sealed owner-private directory has fixed `workspace.pan`
and optional `signals.json` names plus a small nonce manifest. The worker verifies
privacy/seal/budgets, opens that snapshot read-only, and backs it up again into its
own disposable working directory. `PanoramaStore` initialization and threshold
writes happen only on this second copy. Public `database` always names the selected
original file; stage paths are never returned as database identity. The child does
not open the supplied display path. Runtime process deadlines/RSS/CPU/IO policy
remain Core/ResearchPool responsibilities; this library does not claim a Pi timing
or hard process RSS guarantee.

## Bounds and verification

Defaults (all lowering-only): database 128 MiB, 10,000 rows, 1,000,000 total raw
points, 8 MiB per raw record, 16 MiB sidecar, 16 MiB request/result frame and
64 MiB combined serialized working cache. Threshold control itself is at most
64 KiB, checked before enqueue and independently by the child. Queue capacity is
16 (configurable 1..128); the SQLite command queue is 4. Snapshot SQL work defaults
to a 15-second deadline; SQLite has a 2-second busy timeout, and close wait defaults
to 5 seconds. Over-budget work fails explicitly; no partial history is represented
as a complete aggregate. Legacy's own waterfall display limit of 300 is preserved.

Synthetic fixture generation uses the actual existing handler and ApplicationState,
not Rust-generated expected math. It includes raw fractional-Hz grids, IDs above
2^53, composed/decomposed Unicode, offset timestamps with microseconds, -0.0,
subnormal/adjacent f64 values, sidecar rows and multiple threshold lines.
SQL audits compare raw value bytes/f64 bits and schema before/after threshold
changes, with separate extra-table cascade coverage. Tests cover busy reservation,
external concurrent writes, coherent load/read/threshold ordering, malformed IPC,
oversized control/source frames, cancelled close, delayed child completion,
actor panic, retained ownership, reload and reopen.

```sh
python -m unittest tests.test_legacy_rpc
ORI_TEST_PYTHON=/path/to/prepared-core/bin/python cargo test -p ori-legacy --offline -- --test-threads=1
cargo fmt -p ori-legacy --check
cargo clippy -p ori-legacy --all-targets --offline -- -D warnings
python crates/ori-legacy/tests/generate_fixtures.py
python crates/ori-legacy/tests/verify_worker.py --python /path/to/installed/python3.15
python crates/ori-legacy/tests/verify_worker.py --python /path/to/installed/python3.15 --installed-module
python crates/ori-legacy/tests/verify_worker.py --python /path/to/installed/python3.15 --installed-module --shared-io
```

The default Rust process tests use `python -I -c` with an explicit trusted source
bootstrap because the ordinary development interpreter has no installed package.
The separate verification command runs the exact owned worker script with `-I`
against the chosen interpreter's installed algorithms, compares DTOs/error messages
and f64 bits, confirms original source-file bytes, and reports source/module hashes.
This pre-release proof is distinct from testing the final installed `-m` entry
point after wheel refresh. Tests use owned temporary files, no listening server,
hardware, real serial ports or network. Linux is verified; Windows file identity
code and macOS are present but require their platform test runners.

Recorded Linux evidence on 2026-11-11: Rust 1.99.1, 12 Rust integration tests,
9 Python RPC tests, fmt and strict all-target Clippy passed. Python 3.15.1 ran
15 isolated worker requests covering every action, including exact error/float-bit
comparisons and unchanged original source-file SHA256. Its installed six algorithm
modules matched the source at base `111814e3f6bf1d56a57f8a09d8593eb055341e23` byte
for byte. The deterministic fixture SHA256 is
`9a624f47d9ed9d47d4ccb64fea0b42047af010d6ab2f525f6ced273cbe4cc23e`;
regenerating it produced identical bytes. Failed actor-JOIN and foreign-key
preservation regressions were observed before their fixes. No physical-platform
or HTTP-router acceptance is claimed by these library tests.
