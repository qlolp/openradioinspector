# Modernization release verification

The existing `tests.run_release_checks` runner now has two explicit profiles.
The default legacy profile still runs the complete Python/Node/Tk suite, seven
owned loopback fixtures, accelerated monitoring lifecycle checks and, with
`--swift` on macOS, both native Swift suites and the unsigned Xcode build.
`--modern` selects the additive Rust, contract, research and Web checks. A full
release requires **both profiles**; a modern result alone is not native, legacy,
performance or physical acceptance.

```sh
# Linux: use xvfb-run -a for the legacy profile's hidden Tk windows.
python -m tests.run_release_checks --report-dir release-reports/legacy
python -m tests.run_release_checks --modern --research-python /path/to/research-venv/bin/python --report-dir release-reports/modern
# macOS full release: add --swift to each invocation.
```

`--modern --swift` runs the portable generated Swift contract package and the
native SDK's real-Core replay/resume helper. It does not replace the legacy
profile's macOS UI/Xcode checks. The portable checks can also run on Linux with
the pinned Swift toolchain. Missing requested tools fail; they are never
converted into platform skips.

On POSIX, the modern runner uses the canonical preferred temporary parent when
its UTF-8 path is at most 16 bytes; otherwise it uses canonical `/tmp` under the
same limit. This reserves space for nested Core Unix admin socket paths,
including macOS's `/private` aliases. The scope remains owner-only and is cleaned
by the existing lifecycle. Legacy and Windows temporary-directory selection is
unchanged. No global temporary-directory setting or test deadline is changed.

## Prepare dependencies before running

The runner installs nothing and Rust checks use `--locked --offline`. Prepare
Rust 1.99.1 with rustfmt/clippy, Node 26.13.0, the packages pinned in both npm lock
files, and optionally Swift 6.4.2. Keep the existing core environment separate
from the research worker's Python 3.15.1 venv.

The Swift version gate and SDK helper share the same compiler-output validator.
It accepts standalone `Swift version 6.4.2`, standalone `Apple Swift version
6.4.2`, and Apple's `swift-driver version: … Apple Swift version 6.4.2 …`
banner. The driver's own version and parenthesized build metadata cannot satisfy
the compiler pin. A different compiler patch, development suffix, unrelated
version text, or multiple compiler declarations fails before Swift tests/builds.
This parser check does not establish native Swift package or client acceptance.

Supervision fixtures allocate each backend address once per test process and
exclude the host's configured automatic TCP client port range (read-only kernel
query). The allocation regression forces a released address to be offered again
and retains a real foreign listener. Parallel tests remain enabled; production
occupied-port refusals and startup/shutdown deadlines are unchanged. This removes
fixture address reuse and client-port overlap as interference mechanisms; it
does not identify the historical owner of the occupied port in the macOS job.

The legacy scientific bridge strips only the Windows local-disk `\\?\` namespace
before making a read-only SQLite URI. It preserves escaped filename characters
and `mode=ro`; it does not rewrite UNC authorities or open the source for writing.
Portable path examples and the actual Windows scientific/file-budget tests cover
the conversion. A local Linux pass alone does not accept the Windows path.

Owned Python HTTP fixtures explicitly use a 10 ms `serve_forever` selector poll
instead of the standard library's 500 ms idle poll. This reduces idle shutdown
waiting; request/body/SSE timeouts, input sizes, assertions and the 600-second
legacy-suite budget are unchanged. The change applies only to test threads,
not the production server. It is not a throughput or hardware acceptance result.

The legacy SQL writer releases its threshold command's I/O-budget reference
before sending the result. SQL commit/rollback has finished at that point;
remaining snapshot ownership still controls private-file cleanup. A real-worker
regression blocks the budget destructor and proves that neither a successful
commit nor a refused write can reply before that ownership is released.

```sh
python -m pip install --require-hashes -r requirements/core.lock
cargo fetch --locked
npm ci --prefix contracts/generated/typescript --ignore-scripts --no-audit --no-fund
npm ci --prefix clients/web --ignore-scripts --no-audit --no-fund
npm --prefix clients/web run build
python tools/build_modern_web.py
python tools/build_modern_web.py --check
(cd clients/web && npx --no-install playwright install --with-deps chromium)
export CHROMIUM_PATH="$(cd clients/web && node --input-type=module -e 'import { chromium } from "@playwright/test"; console.log(chromium.executablePath())')"
python3.15 -m venv /path/to/research-venv
/path/to/research-venv/bin/python -m pip install --require-hashes -r research/requirements.lock
/path/to/research-venv/bin/python -m pip install --require-hashes -r research/build-requirements.lock
/path/to/research-venv/bin/python -m pip install --no-deps --no-build-isolation ./research
/path/to/research-venv/bin/python -m pip install --no-index --no-deps --no-build-isolation .
```

On Windows the venv interpreter is `Scripts/python.exe`. Pass the interpreter
explicitly with `--research-python`; no interpreter fallback is attempted. The
Windows modern CI lane also passes the explicit [`--windows-llvm` build option](windows-llvm-build.md).
Its existing-tool preflight and actual CMake selections must pass under the
original owned launcher; native compatibility remains to be measured in Windows CI.
The option does not change the default profile or application/package environments. The
research child requires a venv prefix distinct from the core, Python 3.15.1 and
its pinned NumPy 2.5.4, DuckDB 2.0.0 and PyArrow 26.0.1 distributions. Core Python
never imports these research dependencies. The modern CI core interpreter is
selected explicitly per platform below; the production Python compatibility
policy is unchanged.

Package the UI before compiling Core so its generated resource map includes the
completed build. `tools/build_modern_web.py --check` reads the existing packaged
index, manifest and complete file list without building or replacing files. The
modern profile runs this preflight before Cargo; a missing or incomplete package
fails with the two preparation commands above. Core's legacy-only build still
serves its existing 503 response at `/modern/` when no UI was packaged.

`CHROMIUM_PATH` must identify the prepared browser in the child environment,
including when a wrapper gives it a private `HOME` or cache directory. On Windows,
set the same variable to the path returned by the Node command above. CI prepares
the locked browser, verifies that path and publishes it through `GITHUB_ENV`.

## What the modern profile gates

- Check exact Node/Rust versions and the locally installed pinned TypeScript 7
  packages, so npm cannot accidentally use a global compiler.
- Check the already packaged modern UI before starting Cargo compilation.
- Run the contract generator's non-mutating `--check` against the authoritative
  Python module, then the served OpenAPI tests and Python shared-fixture tests.
- Run root-workspace `cargo fmt --all -- --check`, clippy across all targets and
  features with warnings denied, and locked workspace tests with all features.
  The separate `clients/windows` Cargo workspace remains in its own Tauri CI.
- Exercise the freshly built Rust HTTP server with the installed isolated
  scientific worker. Unix checks all fifteen numerical methods against the
  existing service, missing-session validation priority and unchanged event
  history. Windows checks the explicit unavailable-worker response until owned
  process-tree containment exists. Both paths require graceful fixture exit;
  neither creates a skipped test or uses a fake scientific response.
  The calibrated field case shares the physical fixture with all generated
  SDKs, retains unknown uncertainty and checks a 409 for a different chain.
- Compile generated TypeScript and run the shared contract tests with Node TAP.
- Check deterministic Protobuf fixtures without rewriting them, and run their
  Rust and TypeScript decoders against shared valid and malformed frames.
- Discover and run the research worker suite with the explicit isolated
  interpreter. Discovery, executed counts, failures and skips are recorded.
- Run the existing Web package's Vitest script with its JSON reporter, then its
  production build into the runner's private temporary build directory.
- With `--swift`, compile/run portable contract and Protobuf golden tests with
  two jobs. Native application/Xcode acceptance remains in the legacy profile.
- Run the actual compiled Core with its embedded application and an owned
  Playwright Chromium. Require all 1,205 original source frames over negotiated
  binary WebSocket transport, exact source metadata/f64 values, ACKs after
  consumer application, and reconnect from the applied cursor. The functional
  profile explicitly uses Chromium's supported Canvas selection and verifies
  both actual fallback renderer states; it retains the 30-second functional
  bound. With `--swift`, build the pinned native SDK helper in private scratch
  with one job, then require the same replay and a resumed sentinel/ACK proof.
  Builds occur before the functional run. See [the harness contract](client-core-interop.md)
  for the renderer comparison and standalone WebGL2 option.

Framework results must report actual nonzero test counts. Python execution must
match discovery; Rust ignored/filtered/measured cases, Node skipped/cancelled/todo
cases, pending Web cases and unexpected Swift skips all fail the gate. Commands
that exit successfully without the expected test report also fail. Formatting,
compilation and generation stages are reported as commands, not invented tests.

Synthetic Python worker fixtures publish their complete PID marker with a
same-directory rename. Observers cannot see an empty announcement between file
creation and its first write. A controlled paused-writer regression covers both
fixtures; the real HTTP cancellation test still requires the worker to exit and
the original archive writer lease to become available. Its deadlines are unchanged.

Unix Runtime service CLI fixtures use the runner's `ORI_TEST_PYTHON` selection,
matching the supervisor and fork fixtures. A fresh-process wrapper regression
runs the original real startup/status/shutdown test and proves that the selected
interpreter executed. Failed admin readiness retains its primary assertion and
reports bounded error classes and backend milestones only after the original
supervisor Child is waited and the selected backend's writer lease and ports
are released. Raw stderr, credentials, environment values and private paths are
excluded. A real assertion-unwind regression checks the same owned cleanup.
These synthetic checks preserve the existing backend, admin and join deadlines;
native macOS acceptance remains a hosted CI result.

The two supervisor negative-start tests retain separate bounded evidence for
their rejected attempt and subsequent healthy start on the same port. Each
rejected attempt is observed with nonrepairing backend-status and writer/listener
probes; no shutdown or stop intervenes before the healthy start. Final cleanup
follows the observed healthy result, so a leaked backend still makes that start
fail Busy. A released attempt's snapshot is retained before removing its private
trace, so an exit before Python executes the fixture cannot inherit its phases.
Unconfirmed cleanup makes evidence unavailable. Only fixed error classes, phase
names, PIDs, elapsed times and read/truncation flags are reported. The original
200 ms negative and 2 s healthy/shutdown bounds remain. This adds failure evidence;
the cause of the earlier Linux full-workspace readiness timeouts remains
unproven, and a passing focused control does not replace a full gate.

Every command uses the existing `run_command` supervisor, deadline, process group
or Windows Job Object and descendant-cleanup checks. The same runner scope owns
private temporary directories and fixture cleanup. Rust builds/tests and Go/Rayon build workers are capped at two threads; BLAS
uses one thread and Web uses its existing two-worker script. Persistent toolchain stores are explicit while build outputs remain in
the private scope. No private archives or build trees are copied into reports.

## Reports and CI

`--report-dir` preserves a unique `run-*` directory on success, failure, timeout
or interruption. `summary.json` explicitly identifies `profile: legacy|modern`;
`modern-results.json` records each stage as passed, failed or not run, its command,
elapsed time and real test count when applicable. Subsuite JSON files and all
command logs survive cleanup, including partial diagnostics from a failed child.
`hardware_validation` remains `not_run`.
The compact `modern-core-client-interop.json` is retained alongside those
reports. A successful child exit without complete source, renderer, client,
sentinel, immutable-binary, and Core-join proof still fails the stage. Its
`tests_run` is null because this is a functional command rather than a framework
test suite. Performance acceptance remains `not_run`.

The additive `modern-core.yml` matrix runs Rust, research and TypeScript on Linux
x86_64/ARM64, Windows and macOS, including the real-Core Web gate on each. The
native SDK replay/resume helper and portable Swift contract tests run on macOS.
It uses the existing SHA-pinned checkout/Python/Node/artifact actions, hashed Python locks,
`npm ci`, and the native workflow's verified Xcode 27.2 / Swift 6.4.2 selection.
The logical macOS lane uses the `xcode-27` image because `macos-latest` does not
currently provide the required Swift patch. CI uploads diagnostics even if
bootstrap fails; `ci-status.log` records that outcome without claiming tests ran.
Xcode selection also resolves the selected macOS SDK and the explicit
`XcodeDefault` C/C++ compiler paths through `xcrun`. It verifies that their
canonical paths and `stdio.h` belong to the selected Xcode, then compiles a tiny
C-header object with an explicit `-isysroot` and private `HOME`. Only a successful
preflight publishes `SDKROOT`, `CC` and `CXX` to subsequent steps. This preserves
custom Swift toolchain selection while giving bundled SQLite's locked `cc` crate
an explicit `.xctoolchain` compiler path. Portable command-capture tests verify
the setup contract and failure handling; actual compilation remains a hosted
macOS check, not a Linux SDK or target-compatibility claim.
The locked Playwright Chromium is prepared before the profile, and its actual
executable path is passed through `CHROMIUM_PATH` on every OS. Linux audio build
headers are probed first; apt is required only when the prepared host lacks them,
and the final header probe must pass. The existing 12-job Python matrix is
unchanged by this work. The existing browser matrix retains WebGL2 smoke and
context loss/recovery coverage; production rendering defaults are unchanged.

The CI bootstrap selects these exact interpreter patches:

| Matrix row | Core Python | Research Python |
|---|---|---|
| `ubuntu-latest` | 3.12.14 | 3.15.1 |
| `windows-latest` | 3.12.10 | 3.15.1 |
| `macos-latest` (`xcode-27`) | 3.12.10 | 3.15.1 |
| `ubuntu-24.04-arm` | 3.12.14 | 3.15.1 |

The [official actions/python-versions manifest](https://github.com/actions/python-versions/blob/9bc1fe8f6b4befbb1edd3955a1adc8fc086b3e08/versions-manifest.json)
lists 3.12.14 for Linux but no Windows or macOS builds of that patch. It provides
3.12.10 for both of those platforms, and 3.15.1 for all four matrix targets.
These are explicit CI pins, with no interpreter fallback or research-version
change. Missing setup inputs still fail before tests rather than count as a pass.

These checks use owned synthetic fixtures. They do not run the 30-minute target
load or million-sweep history benchmark by default. The existing small baseline
and root-API replay checks can provide bounded per-PR observations; full
performance, installation/rollback and physical instrument gates remain explicit
release acceptance work.

## RT parity: causal evidence before the remaining native failures

The Windows cadence fixture now retains a test-only, bounded ring of the last
8 pressure decisions (at most 8 worker rows each) **before** awaiting original
QoS grant release. It includes the actual reason booleans, previous/current
budget, baseline, epochs, cycles, service and phase ages, plus the synthetic
peer's RBW command-received and prompt-written times on the same monotonic
origin. The existing completed-iteration receipt remains separate: a blocked
drain must not be reported as a completed observation. The owned regression
holds an original work grant and proves this distinction after actual cleanup.
No pressure policy, calibration threshold, epoch rule, wait or deadline changed.

The real-Core Web harness also retains bounded native RAF wait/callback timing
and canvas marker transitions in its existing diagnostic artifacts. It schedules
no additional frames and retains native IDs, timestamps and cancellation. It
reads canvas pixel dimensions without forcing layout. Its final source marker,
5-second assertion, producer workload, Worker/applied ACK and reconnect checks
remain unchanged. A diagnostic pass is not a performance or hardware pass, and
these additions alone do not establish either historical native failure's cause.

The subsequent Windows control fixture waits for its expected canonical
processed-trace pause after `monitor.resume`, before taking the no-artifact
baseline for refused research. A controlled peer holds the ordinary scan after
the command ACK and proves that this independent source commits without any
measurement request. Exact wire order, all refusal assertions and the final
complete artifact/evidence set remain required. No production control behavior
or deadline changes.

The spectrum's undrawn display queue now retains the first maximum and newest
source instead of a stale 64-source FIFO prefix. The current canvas owner and
two-RAF marker protocol are unchanged; coalescing is visible and affects no
archived source, source bits or Worker/applied ACK. See
[web-renderer-display.md](web-renderer-display.md). The 1,205-event native
browser deadline and full offered-to-render checks remain unchanged.
