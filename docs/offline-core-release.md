# Offline Pi/Linux core releases

`tools/release/offline_core.py` assembles **already prepared local inputs**. It
never downloads, installs packages, builds binaries, registers/starts a system
service, switches a `current` pointer, copies an existing archive, or promotes
Rust. Legacy Python remains the generated supervisor default. Existing Linux,
Windows and macOS deployment templates are unchanged.

The tool follows the existing `BackendSpec` and foreground supervisor contracts
in [runtime-modernization.md](runtime-modernization.md), and consumes assets
prepared by `tools/build_modern_web.py` and notices prepared by the existing
release packaging tools. It creates a directory release, not an installer for an
operating system. A transport must preserve Linux file modes and symlinks.

## Prepared input contract

Supply a JSON specification with absolute local paths. Example values must be
replaced with the exact supplied artifact hashes, versions and source revision:

```json
{
  "release_version": "0.4.0-candidate.1",
  "source_revision": "111814e0000000000000000000000000000000000",
  "architecture": "aarch64",
  "ori_core": {
    "path": "/prepared/bin/ori-core",
    "version": "0.1.0",
    "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS",
    "build_revision": null
  },
  "ori_supervisor": {
    "path": "/prepared/bin/ori-supervisor",
    "version": "0.1.0",
    "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS",
    "build_revision": null
  },
  "legacy_runtime": {
    "path": "/prepared/python-core",
    "python_version": "3.12.14",
    "packages": {
      "openradioinspector": "0.4.0.dev0",
      "numpy": "2.5.4",
      "pyserial": "3.5"
    }
  },
  "research_runtime": {
    "path": "/prepared/python-research",
    "python_version": "3.15.1",
    "packages": {
      "openradioinspector": "0.4.0.dev0",
      "ori-research": "0.2.2",
      "numpy": "2.5.4",
      "duckdb": "2.0.0",
      "pyarrow": "26.0.1"
    }
  },
  "libraries": "/prepared/native-libraries",
  "ui_assets": "/prepared/modern-ui",
  "notices": "/prepared/notices",
  "wheels": "/prepared/optional-repair-wheels"
}
```

`libraries` and `wheels` are optional. Prepared runtimes contain their installed
packages already; wheels are optional repair artifacts, not a promise that the
set can reinstall everything. Supplied wheel members, bounds and CRCs are checked.
No wheel is installed or executed. Runtime package maps must describe **all**
installed distributions, using lowercase names with underscores changed to
hyphens. The documented core/worker dependency sets are mandatory; additional
prepared distributions require their exact versions too. Research requires 3.15.

The existing packaging evidence step also checks wheel modern UI files against
`modern-build.json` before writing checksums or notices. It rejects orphan or
missing assets, unsafe/duplicate paths and a missing index; legacy wheels with
no modern UI remain supported. This file-closure check does not establish build
provenance or a complete license inventory.

Each runtime has `bin/python3`, its own interpreter/base prefix, standard library
and site-packages. An ordinary venv pointing at an external base fails. Editable
installs, active `.pth` hooks and site customization also fail. Prepare standalone
prefixes outside this assembler, including locally supplied native dependencies
and public CA bytes. Public certificate PEM is allowed; private-key PEM and
private runtime/state/archive paths are rejected. Notices must contain at least
`LICENSE.txt` and `PYTHON-LICENSE.txt`; supplied notices and distribution license
files are inventoried without an invented completeness attestation.

The bundle source revision is supplied provenance, not something inferred from
an executable filename. Per-executable `build_revision` is an optional exact Git
revision from its build evidence; null means **unknown/WIP**. Functional test
snapshots retain null. Final distributable artifacts must come from a separate
clean exact-revision build. `ori-core --version` is observed; the current
supervisor has no version flag, so its declared build version is bound to its
binary SHA and a real no-argument usage/loader probe. No version interface is
invented. SHA256 is integrity evidence; operator signing remains separate.

## Assembly, inspection and publication

```sh
python3 tools/release/offline_core.py build --spec prepared.json \
  --releases /owned/assembled --report-dir /owned/reports/build
python3 tools/release/offline_core.py verify /owned/assembled/0.4.0-candidate.1 \
  --report-dir /owned/reports/verify
python3 tools/release/offline_core.py install /owned/assembled/0.4.0-candidate.1 \
  --releases /owned/releases --report-dir /owned/reports/install
```

The deterministic `manifest.json` records format version 1, release/source
identity, Linux architecture, component versions/hashes/build revisions, exact
Python/package versions and every payload file, directory, mode and internal
symlink target. `SHA256SUMS` seals the manifest. Neither contains staging paths,
wall-clock build times or runtime credentials. Supplied inputs are never modified.

Copying and hashing stream in 1 MiB blocks. Default hard bounds are 100000 inventory
entries, 2 GiB per file, 8 GiB total payload and 64 MiB manifest; lower bounds can be
passed through the Python API. Traversal, escaping/absolute/broken/cyclic links,
symlinked ancestors, special files, hardlinked published payloads, writable seal
metadata, inconsistent inventories and architecture mismatches fail. Source
hardlinks such as Cargo artifacts are copied into independent destination files.

ELF dependency checks require `/usr/bin/readelf` (`binutils`). Non-OS dependencies
must resolve within the release through safe origin-relative paths or the
release library paths. The explicit OS ABI baseline consists of glibc/pthread/
dl/m/rt/util, the architecture's Linux loader, libgcc and libstdc++; those remain
OS requirements. This is an application bundle, not a complete operating system.
Dynamically loaded device plugins still require their own target acceptance.

Full verification launches fixed argv with no shell, a credential-free
environment, bounded output/deadlines and owned process-group cleanup. From a
new temporary working directory outside the checkout, Python `-I -B` checks
`sys.executable`, `sys.prefix`, `sys.base_prefix`, every imported module path,
exact distributions, core/worker imports and real TLS-context creation. All
Python paths must resolve inside that runtime. Rust help/version and supervisor
usage probes open no devices or archive. The read-only tree is rehashed after
probes. Missing dependencies or tools fail; there is no acceptance skip option.
`inspect` checks integrity only and explicitly reports that portability was not
established.

Build/install use a private staging directory, fsync files/directories, verify,
then Linux `renameat2(RENAME_NOREPLACE)` and fsync the parent. Existing versions
are never replaced, even by an empty-directory race. Exceptions/interruption
remove only the owned unpublished stage. SIGKILL may leave an unpublished stage;
it is not a release and is never selected or silently resumed. A failure after
atomic publication can leave a fully sealed version to inspect; publication is
never equivalent to activation. Diagnostics belong outside release/input/data
paths. Sanitized bounded `events.ndjson` preserves failed attempts after later
success, alongside operation reports; raw child output, environment and source
records are not persisted in those reports.

## Concrete configuration and owned rehearsal

```sh
python3 tools/release/offline_core.py render /owned/releases/0.4.0-candidate.1 \
  --data-dir /owned/data --port 8770 --output /owned/new-config \
  --previous /owned/releases/0.4.0-candidate.0
```

The output contains a default legacy
`supervisor.json`, its exact `legacy-backend.json`, optional
`rollback-backend.json`, and an explicitly named Rust **shadow candidate** spec.
All executables/argv/data paths are concrete; rendering does not create or read
an archive. Config output cannot overlap data or release directories. The Unix
supervisor socket path must fit 107 bytes, so overly long data paths fail before
installation/start. The Rust shadow spec uses distinct public/control ports;
it does not own a writer and is not a successful supervised cutover target.
Replay requires a separately prepared private copy and explicit operator review;
this tool enables neither serial/audio acquisition nor research resource limits.
Rollback uses the same latest data directory, never a restored earlier archive.

The separate explicit `rehearse` command starts real foreground test children,
not system services, on a **new** private scratch/data directory:

```sh
python3 tools/release/offline_core.py rehearse \
  --previous /owned/assembled/old-version --candidate /owned/assembled/new-version \
  --scratch /tmp/ori-rehearsal-new --report-dir /owned/reports/rehearsal
```

It installs two new release paths, starts the real Supervisor with relocated
legacy Python, records synthetic measurements through the real API, performs
orderly cutover, records a latest measurement, explicitly rolls back and checks
unchanged IDs, UTC/raw payloads, negative-zero/adjacent-ULP bits. It then shuts
down, reaps owned children, reacquires actual writer/instance OS locks and checks
listener release while correctly allowing closed TCP TIME_WAIT connections.
It never loads an existing installation's data, selects Rust or opens hardware.
Reports and the new private test archive remain for review, including failures.

## Verification and limits

```sh
python3 -m unittest tests.offline_core_release_checks
python3 -m tests.offline_core_release_acceptance --spec prepared.json \
  --work-dir /tmp/ori-offline-proof-new
```

The first command exercises filesystem/probe failures and ownership boundaries;
synthetic ELF/component fixtures cannot pass the real runtime probes. Linux-only
checks are explicitly skipped on other OSes. The second requires real prepared
inputs and runs real relocation plus two-version Supervisor rehearsal. It is a
separate, explicit Linux CI step after artifact preparation; CI must upload its
reports on failure. Dependency preparation and `binutils` installation belong
outside the assembler. A missing prepared runtime is an error, not a fake pass.

Local Linux x86_64 proof used actual standalone CPython 3.12.14/3.15.1 and locked
NumPy/pyserial/DuckDB/PyArrow, the fresh 111814e Python wheel, and stable functional
Rust executable snapshots whose build revisions remain unknown. Earlier hosted
runtime/TLS, mutable-binary integrity and overlong-socket failures were retained.
Legacy `sounddevice` was absent, so audio readiness/parity is not asserted. The
same supplied component versions in two directories establish software
installation/cutover/rollback mechanics, not cross-version schema downgrade
compatibility. ARM64/Pi hardware/OS/ABI, serial/audio, service-manager operation,
cgroup/disk limits, performance and power-loss acceptance remain unperformed.
Windows/macOS native installers and their CI remain separate.

The [ARM64 preparation and ABI notes](arm64-linux-readiness.md) record exact
official standalone interpreter inputs and a future offline-prefix recipe. The
modern workflow includes native `ubuntu-24.04-arm` software checks alongside its
three existing platforms. This source configuration is not proof of a completed
ARM build, portable prefixes or Pi installation; those remain NOT RUN until the
corresponding native CI/relocation evidence is retained. Ubuntu ARM execution
alone also does not establish compatibility with the selected Pi OS ABI.
