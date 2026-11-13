# Windows modern Cargo LLVM selection

`--windows-llvm` is an explicit build option for `--modern` on Windows. The
modern Windows CI lane selects it; local and legacy defaults are unchanged.
This is an existing-tool candidate for the observed surviving MSVC
`vctip.exe` build helper. Native Windows compatibility and zero-residual closure
are **not proved by the portable tests** and require the native CI run.

```powershell
python -m tests.run_release_checks --modern --windows-llvm --research-python C:\prepared\research\Scripts\python.exe --report-dir release-reports/modern
```

Only `modern-rust-clippy` and `modern-rust-tests` receive a copied build
environment with these selections:

| Child key | Verified existing selection |
| --- | --- |
| `CC_x86_64_pc_windows_msvc`, `CXX_x86_64_pc_windows_msvc` | LLVM 20.1.8 `clang-cl.exe` |
| `AR_x86_64_pc_windows_msvc` | sibling `llvm-lib.exe` |
| `CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER` | Rust 1.99.1's bundled `rust-lld.exe` |
| `CMAKE_GENERATOR` | Ninja |
| `PATH` | prepend the verified LLVM bin and native Ninja directories, retaining the original path |

The caller's environment, other modern commands, running Core/app processes,
packaging and other operating systems receive no LLVM selection. SDK headers and
import libraries, MSVC target ABI, CRT policy, Cargo arguments/profiles, locked
dependencies and the pinned Rust toolchain stay under the existing build rules.
The option adds no LTO or optimization flags. Missing or wrong tools fail; the
runner does not install tools or switch back to a Microsoft compiler/linker.
Conflicting inherited raw-target, host or AWS-LC compiler/CMake selectors fail
before compilation, as do a different explicit `RUSTC` or Cargo build target.
Windows key comparisons ignore case; the copied child map contains one value
per selected logical key. The caller's original keys and values are retained.

Before the first Cargo build, every executable invocation in the preflight uses
the original `run_command` supervisor and owned Windows Job. It resolves existing
x64 PE files, records canonical paths and SHA256 values, and checks LLVM 20.1.8,
CMake 4.4.5, Ninja 1.13.2 and the pinned Rust 1.99.1 host/sysroot. Rust discovery
uses `rustup which` for that explicit installed toolchain, then invokes its
compiler directly. The bundled Rust linker path and its actual version are
recorded. Version/hash receipts are frozen after tool discovery. Tool hashes are
checked before and after the build probes, before each Cargo child, and during
its post-command audit. Replacing an executable at the same path fails.

The hosted Chocolatey Ninja can put an I386 shim on `PATH` even though its
upstream Ninja payload is x64. The resolver recognizes only the exact observed
392,704-byte shim with SHA256
`6a4dd2358fccbb483729091586413cc2f2f74e72d329187f5a6b4cbba3c025ad`
and its valid DOS/PE/I386 headers in the Chocolatey `bin` layout. It reads the
existing `lib/ninja/ninja.nupkg`, requires the published 1.13.2 package SHA512 and
Nuspec identity/version, then verifies the embedded `tools/ninja-win.zip` against
upstream SHA256 `07fc8261b42b20e71d1720b39068c2e14ffcee6396b76fb7a795fb460b78dc65`.
Only an existing `lib/ninja/tools/ninja.exe` matching that payload byte hash and
the original strict x64 PE check can be selected. Package/source provenance is
recorded in `ninja_resolution`; missing or different evidence fails.

This uses Chocolatey's [pinned shim layout](https://github.com/chocolatey/choco/blob/194ac6262ac75571c44aa0a431c3ab6f36fa73be/src/chocolatey/infrastructure.app/services/ShimGenerationService.cs),
the versioned [package receipt](https://community.chocolatey.org/api/v2/Packages(Id='ninja',Version='1.13.2')),
and its [published installer](https://github.com/joachimschmidt557/chocolatey-packages-manual-automatic/blob/968afc9a63f1a9d9b2f30c85ef7248b705ebca6c/automatic/ninja/tools/chocolateyInstall.ps1).
It never executes the shim or installer, extracts files to disk, installs or
downloads a tool, searches alternative locations, or accepts a 32-bit compiler
or build executable. Reads are bounded to a 1 MiB package/archive, 2 MiB payload,
32 entries and 4 MiB declared expanded ZIP contents. Duplicate/case-conflicting entries,
traversal, links, aliases and unsupported compression/encryption fail.

Both Cargo children receive `LLVM bin;verified Ninja directory;original PATH`,
with a shared directory included once. Their actual path lookup must select the
canonical native Ninja. Preflight uses its absolute path, and every produced
CMake cache must select that same `CMAKE_MAKE_PROGRAM`; setting an unsupported
`CMAKE_MAKE_PROGRAM` environment variable is not used. Application, package,
default and non-Cargo environments remain unchanged. Installed package layout
and native tool compatibility still require the Windows preflight to pass.

LLVM 20.1.8's [`CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/llvmorg-20.1.8/llvm/tools/llvm-ar/CMakeLists.txt)
defines `llvm-lib` as an alias of `llvm-ar`, and its
[`driver`](https://github.com/llvm/llvm-project/blob/llvmorg-20.1.8/llvm/tools/llvm-ar/llvm-ar.cpp)
selects the library driver by executable name. That driver has no version flag.
The candidate requires identical existing `llvm-lib`/`llvm-ar` executable hashes,
records the paired `llvm-ar --version`, and actually archives the verified object
with `llvm-lib`. A different distribution layout or differing bytes stop the
candidate; the alias proof is specific to this tagged source and actual pair.
The [hosted image inventory](https://github.com/actions/runner-images/blob/win25-vs2026/20261103.298/images/windows/Windows2025-VS2026-Readme.md)
advertises those tool versions; the inventory alone is not an executable receipt.

The preflight compiles a literal positive C source and verifies a complete x64
COFF object, then separately compiles a literal `#error` source. Only that private
negative probe can expect status 1. Its exact label, compiler hash, command,
LF source bytes and absent output are checked before spawning. After the original Job
closure and JOIN checks, it additionally requires exit 1, the exact compiler
diagnostic marker and no object. A timeout, startup/assignment/cleanup failure,
remaining descendant, crash, zero or any other status remains a failure. There
is no public expected-status option.

A tiny C/C++ CMake/Ninja project includes `windows.h`, creates a static archive
and links an x64 executable against the existing SDK. CMake must identify Clang
with the MSVC ABI/frontend, the verified LLVM archiver, the actual LLD compiler
linker, and Ninja. After each Cargo build the same audit reads every produced
CMake cache and compiler identity file, including the actual `CMAKE_COMMAND`
path and 4.4.5 cache version, rejecting Microsoft compiler, archiver
or linker fallback. Other CMake compiler/assembler paths are recorded. Existing
Windows SDK `rc.exe`/`mt.exe` are permitted explicitly; their versions and hashes
are recorded. Conditionally selected sibling `llvm-rc.exe`/`llvm-mt.exe` must
have the selected LLVM version in their PE version resources; their selected
preflight hashes are frozen and checked during Cargo audits. Neither helper is
required or selected when CMake uses the SDK helper. No SDK files are altered.
LLVM 20.1.8's [clang-cl MSVC discovery](https://github.com/llvm/llvm-project/blob/llvmorg-20.1.8/clang/lib/Driver/ToolChains/MSVC.cpp)
and [LLD COFF discovery](https://github.com/llvm/llvm-project/blob/llvmorg-20.1.8/lld/COFF/Driver.cpp)
support finding existing VS/SDK headers and libraries when `INCLUDE`/`LIB` are
absent. The inherited environment is preserved. Actual discovery and resource
tool integration remain native preflight requirements.

The locked AWS-LC direct C builder can invoke NASM outside CMake. When an existing
`nasm.exe` resolves from the exact Cargo child `PATH`, the preflight records its
actual version and SHA256 and assembles a literal `win64` source into a verified
x64 COFF object under the same owned launcher. Its frozen receipt is checked
before/after Cargo, including when no CMake cache is produced.

NASM remains optional. The existing reqwest/rustls feature chain enables AWS-LC's
packaged prebuilt assembly. When NASM is absent, preflight records absence and
leaves the actual selection pending. After a successful owned Cargo command,
the audit requires the actual AWS-LC `CC` builder, x64 target and NASM-absence
markers, completed-build `libdir` metadata, and all 26 original output objects
matching the pinned `aws-lc-sys 0.47.0` package's `win_x86_64.rs` source list and
`builder/prebuilt-nasm` hashes. The manifest was derived from the package archive
with Cargo.lock SHA256 `8bfbe32ac36b3c428696ce315464181bc4029d689fd58e3f2a4e58c252242f52`.
These objects retain their vendor prebuilt provenance. An unknown assembler or
missing/mismatched prebuilt evidence fails. No feature, tool installation or
assembly flag is changed. Selected-tool receipts are not a trace of every
possible build-script subprocess; full native compatibility and original Job
closure remain CI requirements.

`modern-windows-llvm.json` records preflight evidence; per-Cargo CMake audit JSON
files remain separate from framework test counts. Failure reports retain the
resolved path (at most 1,024 characters), byte count, SHA256 and bounded DOS/PE
signature, offset and numeric machine fields in `rejected_tools` when a hashed
executable fails validation. This diagnostic receipt preserves the rejection;
it does not establish a different host architecture or allow another machine.
No executable contents or environment values are exported by that receipt.
Failure reports also retain the
literal sources and fixed build products, with a 2 MiB per-file and 8 MiB total
artifact bound. The fourteen required preflight logs, plus two when NASM is
available, retain at most 64 KiB each,
with truncation recorded in `modern-windows-llvm/retention.json`. JSON evidence
is bounded to 512 KiB; cache input files are bounded to 64 KiB, with at most 64
caches and 20,000 visited build directories. Reports exclude environment dumps,
private homes, caches and complete build trees. Report errors cannot replace an
original owned-command failure.
Prebuilt assembly evidence reads at most eight AWS-LC build outputs of 512 KiB
each and checks only the fixed 26-object manifest; it retains markers and hashes,
not the build output contents or copied vendor objects.

The existing 15-second empty-Job gate, command deadlines, cleanup requirements
and all modern test oracles remain unchanged. No telemetry-variable guess,
registry preference, process-name termination, allowlist or breakaway is used.
