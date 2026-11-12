# ARM64 Linux software checks and offline inputs

The modern CI matrix includes `ubuntu-24.04-arm` alongside the existing Ubuntu,
Windows and macOS jobs. It uses the same pinned Python 3.12.14/3.15.1, Rust
1.99.1, Node 26.13.0, hashed dependencies, native audio headers, embedded UI,
Chromium preparation and complete modern checks. No ARM-specific skip, smaller
fixture, relaxed timeout or alternate scientific implementation is selected.
Only macOS requests the existing Swift/Xcode checks.

Adding this source configuration does not establish an executed ARM job. ARM
build/runtime results, complete standalone-prefix preparation, relocated offline
installation and Raspberry Pi hardware/performance acceptance remain **NOT RUN**
until their own retained evidence exists. The existing prepared Linux runtimes
and executable proof are x86_64; they cannot be relabelled as ARM64.

## Target OS and ABI boundary

The Pi target is a **64-bit glibc GNU/Linux OS on Raspberry Pi 5**, reporting
`aarch64` from `uname -m`. A 32-bit Pi OS or musl distribution is outside this
bundle's current ELF/loader contract. The pinned ARM wheels require at least
glibc 2.28 as a set: NumPy 2.5.4 has a 2.27 wheel floor, DuckDB 2.0.0 has a 2.26
floor and PyArrow 26.0.1 has a 2.28 floor. Their CPython 3.12/3.15 ARM wheel hashes
are already present in `requirements/core.lock` and `research/requirements.lock`.
The research project still requires Python 3.15.

This dependency floor is not a complete application ABI guarantee. An Ubuntu
24.04 ARM build can require newer GLIBC/GLIBCXX symbols than a Bookworm-based
64-bit Pi OS (glibc 2.36). Select and record the actual Pi OS release, glibc and
libstdc++ baseline before preparing distributable executables and prefixes. Build
against that baseline and verify on it. Neither Bookworm nor a newer Pi OS is
accepted by this CI configuration change alone. The current assembler checks
ELF architecture, loader, dependency names and native execution; it does not
compute a minimum versioned-symbol ABI.

Native audio builds need ARM `pkg-config`/ALSA headers. When linked, non-baseline
libraries such as `libasound.so.2`, Python OpenSSL libraries and wheel-owned
native libraries must resolve inside the supplied release. ALSA device plugins,
configuration, input access and real selected-device capture need separate
target checks. Supply notices for the actual Python/scientific/Rust/native
payload: the existing selected-distribution notice generator is not a complete
SBOM or an automatic ALSA/Rust/research notice inventory.

## Exact standalone interpreter inputs

The following official `astral-sh/python-build-standalone` release assets were
checked through GitHub's release metadata and HTTP HEAD on 2026-11-12. Upload
digests and byte lengths are recorded below; archive bodies were **not downloaded
or rehashed** during this change. Check the bytes before extraction. These are
normal CPython builds, not free-threaded or musl variants. Do not substitute a
moving `latest` release, which already contains newer patch versions.

| Runtime | Official asset | Bytes | SHA256 |
|---|---|---:|---|
| 3.12.14 | [20260929 ARM GNU stripped install-only](https://github.com/astral-sh/python-build-standalone/releases/download/20260929/cpython-3.12.14%2B20260929-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz) | 29203166 | `706111f834ae1557314f48b7293ddc72cc96e952aa75c5d31f56c2b7ddeac3d3` |
| 3.15.1 | [20261021 ARM GNU stripped install-only](https://github.com/astral-sh/python-build-standalone/releases/download/20261021/cpython-3.15.1%2B20261021-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz) | 31702997 | `6efe65c43ae0bdebebeab4607f91a0053dd36d5e51cfd69a791d544e9b122be7` |

The first-party metadata records are [asset 598637300](https://api.github.com/repos/astral-sh/python-build-standalone/releases/assets/598637300)
and [asset 663548553](https://api.github.com/repos/astral-sh/python-build-standalone/releases/assets/663548553).
Those inputs still need native prefix/ABI verification; metadata availability
is not runtime acceptance. Official actions/python-versions metadata also lists
the exact two interpreter versions for Linux 24.04 ARM64; Rust's pinned channel
lists native `aarch64-unknown-linux-gnu` components, and Node's pinned release
lists Linux ARM64. The CI bootstrap must actually install and run these tools and
Chromium; missing tools or browser support remain failures.

## Reproducible prefix preparation

These commands are a future operator-owned preparation recipe, **not an executed
proof**. Run on native AArch64 GNU/Linux matching the chosen target ABI, outside
live service/data directories. Obtain the two exact archives above and reviewed
pure-Python application wheels from a clean exact-revision build first. Keep
their reviewed `SHA256SUMS` beside the application wheels. Use the same revision's
locks and UI/native components. An ordinary venv with an external base prefix is
not a portable-runtime substitute.

Set new private paths; the prepared directory must not exist:

```bash
set -euo pipefail
umask 077
test "$(uname -m)" = aarch64
ORI_SOURCE=/absolute/clean/exact-revision-checkout
ORI_INPUTS=/absolute/private/arm64-inputs
ORI_PREPARED=/absolute/private/new-arm64-prepared
test ! -e "$ORI_PREPARED"

cd "$ORI_INPUTS"
sha256sum --check <<'SHA256'
706111f834ae1557314f48b7293ddc72cc96e952aa75c5d31f56c2b7ddeac3d3  cpython-3.12.14+20260929-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
6efe65c43ae0bdebebeab4607f91a0053dd36d5e51cfd69a791d544e9b122be7  cpython-3.15.1+20261021-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
SHA256
cd "$ORI_INPUTS/application-wheels"
sha256sum --check SHA256SUMS

mkdir -m 700 "$ORI_PREPARED"
mkdir "$ORI_PREPARED/python-core" "$ORI_PREPARED/python-research"
tar -xzf "$ORI_INPUTS/cpython-3.12.14+20260929-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz" \
  --strip-components=1 -C "$ORI_PREPARED/python-core"
tar -xzf "$ORI_INPUTS/cpython-3.15.1+20261021-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz" \
  --strip-components=1 -C "$ORI_PREPARED/python-research"
ORI_PY312="$ORI_PREPARED/python-core/bin/python3"
ORI_PY315="$ORI_PREPARED/python-research/bin/python3"
"$ORI_PY312" -I -c 'import sys; assert sys.version_info[:3] == (3, 12, 14)'
"$ORI_PY315" -I -c 'import sys; assert sys.version_info[:3] == (3, 15, 1)'
"$ORI_PY312" -I -m ensurepip --upgrade
"$ORI_PY315" -I -m ensurepip --upgrade
```

If complete reviewed wheelhouses were not supplied, acquire them during a
separate **connected preparation phase** on this native ARM host. These commands
download wheels and perform no source build. They must not run on an offline
installation. Keep the resulting inputs for checksum review and transfer:

```bash
mkdir "$ORI_INPUTS/core-wheels" "$ORI_INPUTS/research-wheels"
"$ORI_PY312" -I -m pip --isolated download --no-cache-dir \
  --only-binary=:all: --require-hashes \
  --dest "$ORI_INPUTS/core-wheels" -r "$ORI_SOURCE/requirements/core.lock"
"$ORI_PY315" -I -m pip --isolated download --no-cache-dir \
  --only-binary=:all: --require-hashes \
  --dest "$ORI_INPUTS/research-wheels" -r "$ORI_SOURCE/research/requirements.lock"
```

With all inputs supplied, prefix installation itself uses no index, dependency
resolution outside the locked wheelhouses, source compilation or build backend:

```bash
"$ORI_PY312" -I -m pip --isolated install --no-cache-dir --no-index \
  --only-binary=:all: --require-hashes --find-links "$ORI_INPUTS/core-wheels" \
  -r "$ORI_SOURCE/requirements/core.lock"
"$ORI_PY315" -I -m pip --isolated install --no-cache-dir --no-index \
  --only-binary=:all: --require-hashes --find-links "$ORI_INPUTS/research-wheels" \
  -r "$ORI_SOURCE/research/requirements.lock"
"$ORI_PY312" -I -m pip --isolated install --no-cache-dir --no-index --no-deps \
  "$ORI_INPUTS/application-wheels/openradioinspector-0.4.0.dev0-py3-none-any.whl"
"$ORI_PY315" -I -m pip --isolated install --no-cache-dir --no-index --no-deps \
  "$ORI_INPUTS/application-wheels/openradioinspector-0.4.0.dev0-py3-none-any.whl" \
  "$ORI_INPUTS/application-wheels/ori_research-0.2.2-py3-none-any.whl"

# Runtime wheels need neither build backend; remove only these optional tools
# from the new owned prefixes if supplied by the interpreter distribution.
for ORI_PYTHON in "$ORI_PY312" "$ORI_PY315"; do
  "$ORI_PYTHON" -I -m pip --isolated uninstall --yes setuptools wheel
  "$ORI_PYTHON" -I -c 'import importlib.metadata as m, json; print(json.dumps({d.metadata["Name"].lower().replace("_", "-"): d.version for d in m.distributions()}, sort_keys=True))'
done
```

Record **all** resulting distribution versions, including bootstrap pip, in the
two prepared runtime package maps. Do not invent a smaller map or suppress active
`.pth`/site-customization failures. If extra hooks or external native libraries
remain, correct the owned inputs before assembly. Do not copy host credentials,
private keys, data archives or a live runtime into the payload.

Supply the prefixes, clean ARM `ori-core`/`ori-supervisor`, matching UI, actual
native library closure, public CA material and reviewed notices to the existing
[offline assembler](offline-core-release.md). Its build must pass native ELF,
module-prefix, exact-package, TLS-context and integrity probes before publishing.
Then run the separate real offline acceptance in a new private scratch directory:

```bash
python3 -m tests.offline_core_release_acceptance --spec /absolute/prepared-arm64.json \
  --work-dir /absolute/private/new-arm64-offline-proof
```

That later proof covers software relocation, fresh installation and foreground
Supervisor rehearsal. It does not install a system service, promote Rust, prove
physical serial/audio readiness, or establish Pi memory, throughput, latency,
power-loss or continuous-acquisition acceptance. The existing `packaging/pi`
Python installer is unchanged and is not the complete offline Rust/research
installation path.
