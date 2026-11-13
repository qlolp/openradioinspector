"""Opt-in, existing-tool Windows Cargo selection; no installation or gate waiver.

All executable invocations use the release runner's original owned launcher.
The portable tests model that boundary; they are not native Windows evidence.
"""
from dataclasses import dataclass, field
from collections.abc import Mapping
import hashlib
import io
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
from types import MappingProxyType
from xml.etree import ElementTree
import zipfile

BUILD_CHECKS = frozenset(('modern-rust-clippy', 'modern-rust-tests'))
NEGATIVE_NAME = 'modern-windows-llvm-negative'
NEGATIVE_MARKER = 'ORI_WINDOWS_LLVM_EXPECTED_PREPROCESSOR_ERROR'
_NEGATIVE_PROBE = object()
TARGET = 'x86_64-pc-windows-msvc'
LLVM_VERSION = '20.1.8'
NINJA_SHIM_SHA256 = '6a4dd2358fccbb483729091586413cc2f2f74e72d329187f5a6b4cbba3c025ad'
NINJA_NUPKG_SHA512 = ('e9b198480ff29be979dadf0b09be0e3be261f681d03bcdcbb8a4f8fe0a5607e6'
                     'e480b48931ed7e9bc7c06eda658b878e8377eb3ff9a0458d04d21ff8b9c65a1e')
NINJA_ZIP_SHA256 = '07fc8261b42b20e71d1720b39068c2e14ffcee6396b76fb7a795fb460b78dc65'
AWS_LC_CRATE_SHA256 = '8bfbe32ac36b3c428696ce315464181bc4029d689fd58e3f2a4e58c252242f52'
# All win_x86_64.rs .asm objects, read from the archive with the Cargo.lock
# checksum above. NasmBuilder copies these bytes unchanged when NASM is absent.
PREBUILT_NASM = {
    "aes128gcmsiv-x86_64.obj": "d7f4fdeee69623fac4e578a8337894c8560246430ca4471570798e6b5d30ea33",
    "aesni-gcm-avx512.obj": "569e62e6e8d5732abaee872511207693c1344fa19af3b58c2a0ba0559e97f7c9",
    "aesni-gcm-x86_64.obj": "6964357529e797365c774ab0248e5de057db8012b7dedf950945c5b667b87b9d",
    "aesni-sha1-x86_64.obj": "fc7cefa011412ba6aa46427c1eef20482f74fb956f82c02afb5bb1ea09c68dbe",
    "aesni-sha256-x86_64.obj": "2fce161ea1900f88cb8bf9f3aa5fdf12d3c8dc65f9e460ed9831f80b1a5fa781",
    "aesni-x86_64.obj": "27199304f33f218b857e79e642f2e7486c0f3976e3748a91f5e222e18305a6c9",
    "aesni-xts-avx512.obj": "0593952b25de6a7f30acf331116f8d9d97163da1b69394b0f5e5ae0f4d046de1",
    "chacha-x86_64.obj": "ec15e61fab9b611908aff076511ba9384d1c91364f7e6d2719a7005f33cc2bc4",
    "chacha20_poly1305_x86_64.obj": "327a11ce5284565678ec1b43742a725e4fe4b695f089c3b76bd9d33d577bfa46",
    "ghash-ssse3-x86_64.obj": "89e87b083d9a576e4999cbe54d5906e1a48fde22387cccd033ecd50b39d74916",
    "ghash-x86_64.obj": "c2b0e1bc7057cda52f9dcafb61856368c69dc8c6b029d91324c4c8fc987c5f14",
    "md5-x86_64.obj": "d6fe0af0527cf02455cb6a64b69b49b70ab2388f22afc8cd26db47b6d1d7b8aa",
    "p256-x86_64-asm.obj": "36406657cadb5afce1d68133e4ab9bf1ecf8f9f61bf6329f2a5205bf1de3c13d",
    "p256_beeu-x86_64-asm.obj": "e4a790750c10c40b8b56a1e5e3728c1ebb192f101ced4257c086f8a6007a0f69",
    "rdrand-x86_64.obj": "2cc6bae89792590cc17edf4421397c0b03396ba75213690c4481814d2404c674",
    "rsaz-2k-avx512.obj": "60a9ad5a97951fc197ce5d3eceea6b0deccf502c52cfab0e709cff2a2bf1a76d",
    "rsaz-3k-avx512.obj": "9b70942025bad903e6222c987e454fe851d4e30d9aa76e41bdbd6ca6c0e9b14b",
    "rsaz-4k-avx512.obj": "483c6b782a80bd4d570250c7e291eef6363bbd4fc8a7a3ed3d79a1ded1717420",
    "rsaz-avx2.obj": "401fce70b9161636013799180472ef7df2a8af30619f9b7ac830af2caaa12b71",
    "sha1-x86_64.obj": "151fa77918ebe8637a306f38c65936fd67ade05e1bae8892407eedf3cce4f35c",
    "sha256-x86_64.obj": "30a268baee19940e4385f3621d57ffef5941158449e0866d375b2714086446e5",
    "sha512-x86_64.obj": "faea2522934142a14a7de16a132c975d1337fa39931e038562da5ef791242c7f",
    "trampoline-x86_64.obj": "c45c7e279e8374d3dc47923b1380e4a99d7103646b80125226fcbd2b8257f30b",
    "vpaes-x86_64.obj": "0e211b2615c0af85db7e56fc840e7ea7c67e95e8e9c8bd20b5a62e634bce0fc9",
    "x86_64-mont.obj": "24b65b5bfcb03c95033311f01fbff308533d946b90afc9dd763eaf8d11adf8b0",
    "x86_64-mont5.obj": "96a7eccb65cbb6c2556170ad1ad0d07f01ed0214f38a594400c62bdc37d62808"
}
ARTIFACT_LIMIT = 2 * 1024 * 1024
TEXT_LIMIT = 64 * 1024
CACHE_LIMIT = 64
LLVM_AR_SOURCE = 'https://github.com/llvm/llvm-project/blob/llvmorg-20.1.8/llvm/tools/llvm-ar/'
PROVENANCE = {
    'image_inventory': 'https://github.com/actions/runner-images/blob/win25-vs2026/20261103.298/images/windows/Windows2025-VS2026-Readme.md',
    'llvm_lib_alias': LLVM_AR_SOURCE + 'CMakeLists.txt',
    'llvm_lib_dispatch': LLVM_AR_SOURCE + 'llvm-ar.cpp',
    'rust_toolchain': 'rust-toolchain.toml: 1.99.1',
    'chocolatey_shim': 'https://github.com/chocolatey/choco/blob/194ac6262ac75571c44aa0a431c3ab6f36fa73be/src/chocolatey/infrastructure.app/services/ShimGenerationService.cs',
    'ninja_package': "https://community.chocolatey.org/api/v2/Packages(Id='ninja',Version='1.13.2')",
    'ninja_package_layout': 'https://github.com/joachimschmidt557/chocolatey-packages-manual-automatic/blob/968afc9a63f1a9d9b2f30c85ef7248b705ebca6c/automatic/ninja/tools/chocolateyInstall.ps1',
    'ninja_upstream': 'https://github.com/ninja-build/ninja/releases/tag/v1.13.2',
    'cmake_compiler_list': 'https://github.com/Kitware/CMake/blob/v4.4.5/Help/variable/CMAKE_LANG_COMPILER.rst',
    'cmake_identification_arguments': 'https://github.com/Kitware/CMake/blob/v4.4.5/Modules/CMakeDetermineCompilerId.cmake',
    'cmake_linker_type': 'https://github.com/Kitware/CMake/blob/v4.4.5/Help/variable/CMAKE_LINKER_TYPE.rst',
    'clang_cl_linker_selection': 'https://github.com/llvm/llvm-project/blob/llvmorg-20.1.8/clang/lib/Driver/ToolChains/MSVC.cpp',
    'locked_cmake_toolchain': 'Cargo.lock: cmake 0.1.58; src/lib.rs: Config::build / getenv_target_os',
}
ARTIFACTS = ('positive.c', 'positive.obj', 'positive.lib', 'negative.c',
             'llvm-toolchain.cmake',
             'positive.asm', 'nasm-positive.obj',
             'cmake/CMakeLists.txt', 'cmake/positive.c', 'cmake/main.cpp',
             'cmake-build/ori_llvm_probe.exe', 'cmake-build/ori_llvm_probe.lib')
LOG_SUFFIXES = ('rustc-path', 'rustc-version', 'rustc-sysroot', 'clang-version', 'archive-version',
                'linker-version', 'rust-linker-version', 'cmake-version', 'ninja-version',
                'nasm-version', 'nasm-positive', 'positive', 'negative', 'archive', 'cmake-configure', 'cmake-build')


class LLVMError(ValueError):
    def __init__(self, message, *, rejected_receipt=None):
        super().__init__(message)
        self.rejected_receipt = rejected_receipt


@dataclass(frozen=True)
class VerifiedTools(Mapping):
    """Read-only path and version/hash receipts from one successful preflight."""
    paths: Mapping
    receipts: Mapping
    assembler: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'paths', MappingProxyType(dict(self.paths)))
        object.__setattr__(self, 'receipts', MappingProxyType({
            key: MappingProxyType(dict(value)) for key, value in self.receipts.items()}))
        object.__setattr__(self, 'assembler', MappingProxyType(dict(self.assembler)))

    def __getitem__(self, key):
        return self.paths[key]

    def __iter__(self):
        return iter(self.paths)

    def __len__(self):
        return len(self.paths)


def _selector_conflicts(parent, tools):
    # cc 1.11.1 / cmake 0.1.58 prefer raw-target names to underscore names.
    # aws-lc-sys 0.47.0 also exports its own and HOST compiler selectors.
    expected = {}
    for variable, tool in (('CC', 'clang_cl'), ('CXX', 'clang_cl'), ('AR', 'llvm_lib')):
        expected[variable + '_' + TARGET] = tools[tool]
        if variable != 'AR':
            for base in ('HOST_' + variable, 'AWS_LC_SYS_' + variable, 'AWS_LC_SYS_HOST_' + variable):
                expected[base] = tools[tool]
                expected[base + '_' + TARGET.replace('-', '_')] = tools[tool]
    for base, value in (('CMAKE_GENERATOR', 'Ninja'), ('CMAKE', tools.get('cmake'))):
        for key in (base + '_' + TARGET, base + '_' + TARGET.replace('-', '_'), 'HOST_' + base,
                    'AWS_LC_SYS_' + base, 'AWS_LC_SYS_' + base + '_' + TARGET.replace('-', '_')):
            expected[key] = value
        if base == 'CMAKE':
            expected[base] = value
    expected.update(RUSTC=tools.get('rustc'), CARGO_BUILD_TARGET=TARGET)
    for base in ('CMAKE_TOOLCHAIN_FILE', 'AWS_LC_SYS_CMAKE_TOOLCHAIN_FILE',
                 'AWS_LC_SYS_HOST_CMAKE_TOOLCHAIN_FILE'):
        for key in (base, base + '_' + TARGET, base + '_' + TARGET.replace('-', '_')):
            expected[key] = tools.get('cmake_toolchain')
    for key in ('HOST_CMAKE_TOOLCHAIN_FILE', 'TARGET_CMAKE_TOOLCHAIN_FILE'):
        expected[key] = tools.get('cmake_toolchain')
    for key, wanted in expected.items():
        for actual_key, actual in parent.items():
            if actual_key.casefold() != key.casefold():
                continue
            equal = actual == wanted if 'GENERATOR' in key or key == 'CARGO_BUILD_TARGET' else wanted is not None and _same_path(actual, wanted)
            if not equal:
                raise LLVMError(f'{key}: inherited selector conflicts with the verified Windows LLVM candidate')


def _set_windows_key(environment, key, value):
    # os.environ uppercases Windows keys. Avoid duplicate case variants in the
    # plain copied map passed to CreateProcess, while preserving the caller map.
    for existing in tuple(environment):
        if existing.casefold() == key.casefold():
            del environment[existing]
    environment[key] = value


def environment_for(parent, *, platform, modern, opt_in, check_name, tools):
    child = parent.copy()
    if platform != 'win32' or not modern or not opt_in or check_name not in BUILD_CHECKS:
        return child
    if not tools.get('cmake_toolchain') or not ntpath.isabs(tools['cmake_toolchain']):
        raise LLVMError('selected Cargo child requires the absolute owned CMake toolchain')
    _selector_conflicts(parent, tools)
    for key, value in {'CC_x86_64_pc_windows_msvc': tools['clang_cl'],
                       'CXX_x86_64_pc_windows_msvc': tools['clang_cl'],
                       'AR_x86_64_pc_windows_msvc': tools['llvm_lib'],
                       'CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER': tools['rust_lld'],
                       'CMAKE_GENERATOR': 'Ninja',
                       'CMAKE_TOOLCHAIN_FILE_x86_64_pc_windows_msvc': tools['cmake_toolchain']}.items():
        _set_windows_key(child, key, value)
    # A Windows PATH delimiter is intentional, including in portable scope tests.
    inherited_path = next((value for key, value in parent.items() if key.casefold() == 'path'), '')
    prefixes = [ntpath.dirname(tools['clang_cl'])]
    if tools.get('ninja') and not _same_path(ntpath.dirname(tools['ninja']), prefixes[0]):
        prefixes.append(ntpath.dirname(tools['ninja']))
    _set_windows_key(child, 'PATH', ';'.join(prefixes) + (';' + inherited_path if inherited_path else ''))
    return child


def _bounded(path, limit=TEXT_LIMIT):
    path = Path(path)
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > limit:
        raise LLVMError(f'{path.name}: expected bounded regular file')
    with path.open('rb') as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise LLVMError(f'{path.name}: file exceeded byte bound')
    return value


def _receipt(path, *, provenance):
    path = Path(path).resolve(strict=True)
    if not stat.S_ISREG(path.stat().st_mode):
        raise LLVMError(f'{path.name}: expected regular file')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'path': str(path), 'bytes': path.stat().st_size,
            'sha256': digest.hexdigest(), 'provenance': provenance}


def _toolchain_receipt(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise LLVMError('CMake toolchain must remain an owned absolute regular file')
    data = _bounded(path)
    return {'path': str(path.resolve(strict=True)), 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
            'provenance': 'owned CMake documented compiler-list toolchain; identification and target linker selection'}


def _prepare_toolchain(root, tools):
    # Required compiler options are documented list items. CMake derives ARG1
    # itself and forwards it to direct compiler-identification invocations.
    def literal(key):
        value = tools[key].replace('\\', '/')
        if not ntpath.isabs(value) or any(token in value for token in (';', ']=]', '\r', '\n', '\0')):
            raise LLVMError('verified tool path cannot be represented as a CMake compiler-list item')
        return '[=[' + value + ']=]'
    compiler = literal('clang_cl')
    text = ''.join('set(CMAKE_' + language + '_COMPILER ' + compiler + ' -fuse-ld=lld)\n'
                   for language in ('C', 'CXX'))
    text += 'set(CMAKE_LINKER_TYPE LLD CACHE STRING "" FORCE)\n'
    for key, tool in (('CMAKE_LINKER', 'lld_link'), ('CMAKE_LINKER_LLD', 'lld_link'),
                      ('CMAKE_AR', 'llvm_lib'), ('CMAKE_MAKE_PROGRAM', 'ninja')):
        text += 'set(' + key + ' ' + literal(tool) + ' CACHE FILEPATH "" FORCE)\n'
    path = Path(root) / 'llvm-toolchain.cmake'
    with path.open('xb') as output:
        output.write(text.encode('utf-8'))
    tools['cmake_toolchain'] = str(path.resolve(strict=True))
    return _toolchain_receipt(tools['cmake_toolchain'])


def executable_receipt(path, *, name, provenance):
    diagnostic = None
    try:
        path = Path(path)
        if not path.is_absolute() or path.name.lower() != name.lower():
            raise LLVMError(f'{name}: expected existing absolute executable path')
        result = _receipt(path, provenance=provenance)
        diagnostic = {'path': result['path'][:1024], 'path_truncated': len(result['path']) > 1024,
                      'bytes': result['bytes'], 'sha256': result['sha256'],
                      'dos_signature_hex': None, 'pe_offset': None,
                      'pe_signature_hex': None, 'machine_code': None}
        # Read the bounded DOS/PE headers, never load or execute the image here.
        with path.open('rb') as source:
            dos = source.read(64)
            diagnostic['dos_signature_hex'] = dos[:2].hex()
            if len(dos) != 64 or dos[:2] != b'MZ':
                raise LLVMError(f'{name}: invalid PE header')
            offset = struct.unpack_from('<I', dos, 60)[0]
            diagnostic['pe_offset'] = offset
            if not 64 <= offset <= min(result['bytes'] - 6, 1024 * 1024):
                raise LLVMError(f'{name}: invalid PE offset')
            source.seek(offset)
            header = source.read(6)
            diagnostic['pe_signature_hex'] = header[:4].hex()
            if len(header) == 6:
                diagnostic['machine_code'] = struct.unpack_from('<H', header, 4)[0]
            if header != b'PE\0\0\x64\x86':
                raise LLVMError(f'{name}: expected x64 PE executable')
        return {**result, 'machine': 'x86_64'}
    except (OSError, ValueError) as error:
        if isinstance(error, LLVMError):
            error.rejected_receipt = diagnostic
            raise
        raise LLVMError(f'{name}: executable is unavailable ({type(error).__name__})',
                        rejected_receipt=diagnostic) from error


def _checked_chocolatey_file(path, root, limit):
    if (not root.is_absolute() or '..' in root.parts or len(str(path)) > 1024 or
            not _same_path(str(root), str(root.resolve(strict=True)))):
        raise LLVMError('Chocolatey Ninja package root is not canonical and bounded')
    current = root
    for part in ('', *path.relative_to(root).parts):
        current = current / part if part else current
        if (current.is_symlink() or getattr(current, 'is_junction', lambda: False)() or
                not _same_path(str(current), str(current.resolve(strict=True)))):
            raise LLVMError('Chocolatey Ninja package path contains a link or alias')
    return _bounded(path, limit)


def _ninja_zip_member(data, name, limit):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if not 0 < len(entries) <= 32:
            raise LLVMError('Chocolatey Ninja ZIP entry count exceeded its bound')
        names, total = set(), 0
        for entry in entries:
            parts = entry.filename.rstrip('/').split('/')
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.filename.casefold() in names or '\\' in entry.filename or ':' in entry.filename or
                    any(part in ('', '.', '..') for part in parts) or mode not in (0, stat.S_IFREG, stat.S_IFDIR) or
                    entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise LLVMError('Chocolatey Ninja ZIP has duplicate, unsafe, linked or unsupported entries')
            names.add(entry.filename.casefold())
            total += entry.file_size
            if entry.file_size > ARTIFACT_LIMIT or total > ARTIFACT_LIMIT * 2:
                raise LLVMError('Chocolatey Ninja ZIP expanded size exceeded its bound')
        try:
            member = archive.getinfo(name)
        except KeyError as error:
            required = name if name in ('ninja.nuspec', 'tools/ninja-win_x32.zip', 'ninja.exe') else '<required member>'
            raise LLVMError(f'required pinned Ninja ZIP member {required!r} is missing') from error
        if member.is_dir() or member.file_size > limit:
            raise LLVMError('Chocolatey Ninja ZIP member exceeded its bound')
        with archive.open(member) as source:
            payload = source.read(limit + 1)
        if len(payload) != member.file_size or len(payload) > limit:
            raise LLVMError('Chocolatey Ninja ZIP member is truncated or oversized')
        return payload


def _resolve_chocolatey_ninja(shim, environment, evidence):
    """Read one pinned installed package; never execute the shim or installer."""
    try:
        root_value = next((value for key, value in environment.items() if key.casefold() == 'chocolateyinstall'),
                          'C:/ProgramData/chocolatey')
        root = Path(root_value)
        shim = Path(shim)
        if not _same_path(str(shim), str(root / 'bin/ninja.exe')):
            raise LLVMError('observed Ninja shim does not match the Chocolatey bin layout')
        shim_bytes = _checked_chocolatey_file(shim, root, 392704)
        if len(shim_bytes) != 392704 or hashlib.sha256(shim_bytes).hexdigest() != NINJA_SHIM_SHA256:
            raise LLVMError('Chocolatey Ninja shim no longer matches the observed identity')
        package = root / 'lib/ninja/ninja.nupkg'
        evidence['package_path'] = str(package)
        data = _checked_chocolatey_file(package, root, 1024 * 1024)
        package_hash = hashlib.sha512(data).hexdigest()
        if package_hash != NINJA_NUPKG_SHA512:
            raise LLVMError('installed Chocolatey Ninja package SHA512 differs from the published 1.13.2 package')
        evidence['package_sha512'] = package_hash
        spec = _ninja_zip_member(data, 'ninja.nuspec', TEXT_LIMIT).decode('utf-8-sig')
        if '<!DOCTYPE' in spec.upper() or '<!ENTITY' in spec.upper():
            raise LLVMError('Chocolatey Ninja package metadata contains unsupported declarations')
        document = ElementTree.fromstring(spec)
        metadata = [node for node in document if node.tag.rsplit('}', 1)[-1] == 'metadata']
        if document.tag.rsplit('}', 1)[-1] != 'package' or len(metadata) != 1:
            raise LLVMError('Chocolatey Ninja package metadata is missing or ambiguous')
        for key, expected in (('id', 'ninja'), ('version', '1.13.2')):
            values = [(node.text or '').strip() for node in metadata[0] if node.tag.rsplit('}', 1)[-1] == key]
            if values != [expected]:
                raise LLVMError('Chocolatey Ninja package identity/version does not match the pinned package')
        upstream_zip = _ninja_zip_member(data, 'tools/ninja-win_x32.zip', 1024 * 1024)
        if hashlib.sha256(upstream_zip).hexdigest() != NINJA_ZIP_SHA256:
            raise LLVMError('Chocolatey Ninja archive differs from the official upstream 1.13.2 release')
        evidence['upstream_zip_sha256'] = NINJA_ZIP_SHA256
        expected = _ninja_zip_member(upstream_zip, 'ninja.exe', ARTIFACT_LIMIT)
        target = root / 'lib/ninja/tools/ninja.exe'
        installed = _checked_chocolatey_file(target, root, ARTIFACT_LIMIT)
        if hashlib.sha256(installed).digest() != hashlib.sha256(expected).digest():
            raise LLVMError('installed canonical Ninja executable differs from the verified upstream payload')
        receipt = executable_receipt(target, name='ninja.exe', provenance='exact pinned Chocolatey/upstream 1.13.2 payload')
        if not _same_path(receipt['path'], str(target)):
            raise LLVMError('canonical Ninja receipt escaped the checked installed package path')
        if receipt['sha256'] != hashlib.sha256(expected).hexdigest():
            raise LLVMError('canonical Ninja changed between the payload check and final executable receipt')
        evidence.update(status='verified_package_payload', executable_path=receipt['path'],
                        payload_sha256=receipt['sha256'])
        return receipt
    except LLVMError:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise LLVMError(f'Chocolatey Ninja installed package validation failed ({type(error).__name__})') from error


def verify_tools(tools, *, environment=None):
    if type(tools) is not VerifiedTools:
        raise LLVMError('Cargo LLVM selection has no frozen preflight receipts')
    if 'cmake_toolchain' not in tools:
        raise LLVMError('Cargo LLVM selection has no frozen owned CMake toolchain')
    for key, path in tools.items():
        actual = (_toolchain_receipt(path) if key == 'cmake_toolchain' else
                  executable_receipt(path, name=Path(path).name, provenance='immutable preflight tool check'))
        if (key not in tools.receipts or actual['sha256'] != tools.receipts[key]['sha256'] or
                not _same_path(actual['path'], tools.receipts[key]['path'])):
            raise LLVMError(f'{key}: selection changed after its frozen preflight receipt')
    if environment is not None:
        _selector_conflicts(environment, tools)
        actual = next((value for key, value in environment.items()
                       if key.casefold() == 'cmake_toolchain_file_x86_64_pc_windows_msvc'), '')
        if not _same_path(actual, tools['cmake_toolchain']):
            raise LLVMError('actual Cargo child does not select the frozen owned CMake toolchain')
    if environment is not None and 'ninja' in tools:
        actual = shutil.which('ninja.exe', path=environment.get('PATH'))
        if actual is None or not _same_path(str(Path(actual).resolve()), tools['ninja']):
            raise LLVMError('actual Cargo child PATH does not select the verified canonical Ninja')
    if environment is not None and 'nasm' in tools:
        actual = shutil.which('nasm.exe', path=environment.get('PATH'))
        if actual is None or not _same_path(str(Path(actual).resolve()), tools['nasm']):
            raise LLVMError('NASM selection from the actual Cargo child PATH is unknown or changed')
    elif environment is not None and tools.assembler.get('status') == 'nasm_absent_prebuilt_pending':
        if shutil.which('nasm.exe', path=environment.get('PATH')) is not None:
            raise LLVMError('NASM appeared after preflight; actual assembler selection changed')


def _coff(data):
    if len(data) < 20:
        raise LLVMError('positive object has no complete COFF header')
    machine, sections, _, _, _, optional, _ = struct.unpack_from('<HHIIIHH', data)
    if machine != 0x8664 or not 0 < sections <= 256 or optional or len(data) < 20 + sections * 40:
        raise LLVMError('positive object is not a complete x64 COFF object')
    for index in range(sections):
        size, start = struct.unpack_from('<II', data, 20 + index * 40 + 16)
        if size and (start < 20 + sections * 40 or start + size > len(data)):
            raise LLVMError('positive object has a truncated COFF section')


def validate_probe(exit_code, output, obj, *, negative):
    obj = Path(obj)
    if type(exit_code) is not int:
        raise LLVMError('compiler status must be an integer exit code')
    if negative:
        diagnostic = re.search(r'\berror:\s*' + re.escape(NEGATIVE_MARKER) + r'\s*$', output, re.M)
        if exit_code != 1 or diagnostic is None or obj.exists() or obj.is_symlink():
            raise LLVMError('negative probe requires exit 1, the literal diagnostic, and no object')
        return {'status': 'expected_compiler_error', 'exit_code': 1, 'marker': NEGATIVE_MARKER,
                'object_created': False}
    if exit_code != 0:
        raise LLVMError('positive compiler probe did not exit zero')
    try:
        _coff(_bounded(obj, ARTIFACT_LIMIT))
        return {'status': 'compiled', **_receipt(obj, provenance='literal positive compile')}
    except OSError as error:
        raise LLVMError('positive object is unavailable') from error


def validate_archive(archive, obj=None):
    data = _bounded(archive, ARTIFACT_LIMIT)
    expected = _bounded(obj, ARTIFACT_LIMIT) if obj is not None else None
    if expected is not None:
        _coff(expected)
    if not data.startswith(b'!<arch>\n'):
        raise LLVMError('probe library is not a COFF archive')
    position, found = 8, False
    while position < len(data):
        header = data[position:position + 60]
        if len(header) != 60 or header[58:] != b'`\n':
            raise LLVMError('probe archive has a malformed member header')
        try:
            size = int(header[48:58].decode('ascii').strip())
        except (UnicodeError, ValueError) as error:
            raise LLVMError('probe archive has a malformed member size') from error
        start, end = position + 60, position + 60 + size
        if size < 0 or end > len(data):
            raise LLVMError('probe archive has a truncated member')
        if expected is not None:
            found |= data[start:end] == expected
        else:
            try:
                _coff(data[start:end])
                found = True
            except LLVMError:
                pass  # Archive symbol/name tables are not object members.
        position = end + size % 2
    if position != len(data) or not found:
        raise LLVMError('probe archive does not contain the exact verified x64 object')
    return {'status': 'archived', **_receipt(archive, provenance='llvm-lib exact-object archive probe')}


@dataclass(frozen=True)
class _NegativeProbe:
    authority: object
    source: Path
    obj: Path
    compiler: str
    sha256: str

    @property
    def command(self):
        return [self.compiler, '/nologo', '/c', '/MD', '/Fo' + str(self.obj), str(self.source)]


def prepare_negative_probe(directory, tools):
    root = Path(directory).resolve() / 'modern-windows-llvm'
    root.mkdir(exist_ok=True)
    source, obj = root / 'negative.c', root / 'negative.obj'
    if obj.exists():
        raise LLVMError('negative probe output already exists')
    source.write_bytes(('#error ' + NEGATIVE_MARKER + '\n').encode('ascii'))
    compiler = executable_receipt(tools['clang_cl'], name='clang-cl.exe', provenance='verified preflight compiler')
    return _NegativeProbe(_NEGATIVE_PROBE, source, obj, compiler['path'], compiler['sha256'])


def validate_negative_invocation(probe, command, *, name, directory, platform):
    """Validate the private exception before the original owned launcher spawns."""
    if (platform != 'win32' or name != NEGATIVE_NAME or type(probe) is not _NegativeProbe or
            probe.authority is not _NEGATIVE_PROBE):
        raise LLVMError('intentional compiler error is restricted to the private Windows LLVM probe')
    root = Path(directory).resolve() / 'modern-windows-llvm'
    if (probe.source != root / 'negative.c' or probe.obj != root / 'negative.obj' or
            list(command) != probe.command or probe.obj.exists() or
            _bounded(probe.source) != ('#error ' + NEGATIVE_MARKER + '\n').encode('ascii')):
        raise LLVMError('negative probe invocation does not match its literal preflight source/output')
    current = executable_receipt(probe.compiler, name='clang-cl.exe', provenance='negative probe compiler')
    if current['sha256'] != probe.sha256:
        raise LLVMError('negative probe compiler changed after verification')


def _same_path(actual, expected):
    # Windows CMake normalizes backslashes and drive case in its cache.
    return ntpath.normcase(ntpath.normpath(actual)) == ntpath.normcase(ntpath.normpath(expected))


def _windows_file_version(path):
    """Read a helper's version resource; this neither loads nor runs its image."""
    import ctypes
    if sys.platform != 'win32':
        raise LLVMError('Windows helper version resources require Windows')
    api = ctypes.WinDLL('version', use_last_error=True)
    uint32 = ctypes.c_uint32
    for name, args, result in (
        ('GetFileVersionInfoSizeW', [ctypes.c_wchar_p, ctypes.POINTER(uint32)], uint32),
        ('GetFileVersionInfoW', [ctypes.c_wchar_p, uint32, uint32, ctypes.c_void_p], ctypes.c_int32),
        ('VerQueryValueW', [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p),
                            ctypes.POINTER(uint32)], ctypes.c_int32),
    ):
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    unused = uint32()
    size = api.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
    if not 52 <= size <= TEXT_LIMIT:
        raise LLVMError('Windows helper has no bounded fixed version resource')
    buffer = ctypes.create_string_buffer(size)
    value, length = ctypes.c_void_p(), uint32()
    if (not api.GetFileVersionInfoW(str(path), 0, size, buffer) or
            not api.VerQueryValueW(buffer, '\\', ctypes.byref(value), ctypes.byref(length)) or
            not 52 <= length.value <= size or not value.value):
        raise LLVMError('Windows helper version resource query failed')
    fixed = ctypes.cast(value, ctypes.POINTER(uint32 * 13)).contents
    if fixed[0] != 0xFEEF04BD:
        raise LLVMError('Windows helper version resource signature is invalid')
    return '.'.join(str(part) for part in (fixed[2] >> 16, fixed[2] & 0xffff,
                                          fixed[3] >> 16, fixed[3] & 0xffff))


def validate_cmake(build, tools, *, preflight=False):
    build = Path(build)
    cache = _bounded(build / 'CMakeCache.txt').decode('utf-8', errors='strict')
    values = {}
    for line in cache.splitlines():
        if line.startswith(('#', '//')) or not line:
            continue
        match = re.fullmatch(r'([^:=]+):[^=]+=(.*)', line)
        if match:
            key, value = match.groups()
            if key in values:
                raise LLVMError('duplicate CMake cache selection')
            values[key] = value
    if values.get('CMAKE_GENERATOR') != 'Ninja':
        raise LLVMError('CMake did not select Ninja')
    if [values.get('CMAKE_CACHE_' + component + '_VERSION') for component in ('MAJOR', 'MINOR', 'PATCH')] != ['4', '4', '5']:
        raise LLVMError('actual CMake cache version is not the verified 4.4.5')
    if values.get('CMAKE_LINKER_TYPE') != 'LLD':
        raise LLVMError('CMake did not explicitly select linker type LLD')
    if not tools.get('cmake_toolchain') or not _same_path(values.get('CMAKE_TOOLCHAIN_FILE', ''), tools['cmake_toolchain']):
        raise LLVMError('CMake did not select the frozen owned toolchain')
    toolchain = _toolchain_receipt(tools['cmake_toolchain'])
    if type(tools) is VerifiedTools and toolchain['sha256'] != tools.receipts['cmake_toolchain']['sha256']:
        raise LLVMError('CMake toolchain differs from its frozen preflight receipt')
    expected = {'CMAKE_AR': 'llvm_lib', 'CMAKE_LINKER': 'lld_link', 'CMAKE_LINKER_LLD': 'lld_link',
                'CMAKE_MAKE_PROGRAM': 'ninja', 'CMAKE_COMMAND': 'cmake'}
    identities = {}
    # A documented normal-variable compiler list need not create compiler cache
    # entries. CMake's generated identity files hold the actual path and ARG1.
    for language in ('C', 'CXX'):
        key = 'CMAKE_' + language + '_COMPILER'
        files = sorted((build / 'CMakeFiles').glob('*/CMake' + language + 'Compiler.cmake'))
        if len(files) > 1 or (len(files) != 1 and (language == 'C' or values.get(key))):
            raise LLVMError(f'CMake {language} compiler identity is absent or ambiguous')
        if not files:
            continue
        identity = _bounded(files[0]).decode('utf-8', errors='strict')
        paths = re.findall(r'set\(' + key + r' "([^"]*)"\)', identity)
        if len(paths) != 1 or not _same_path(paths[0], tools['clang_cl']):
            raise LLVMError(f'CMake {language} actual compiler is not the verified Clang')
        if key in values and not _same_path(values[key], paths[0]):
            raise LLVMError(f'CMake {language} cache and actual compiler identity disagree')
        values[key] = paths[0]
        expected[key] = 'clang_cl'
        identities[language] = identity
    selected = {}
    for key, tool in expected.items():
        if not _same_path(values.get(key, ''), tools[tool]):
            raise LLVMError(f'{key}: CMake did not select the verified LLVM/Ninja tool')
        selected[key] = executable_receipt(values[key], name=Path(tools[tool]).name,
                                           provenance='actual CMake cache selection')
        if type(tools) is VerifiedTools and selected[key]['sha256'] != tools.receipts[tool]['sha256']:
            raise LLVMError(f'{key}: executable differs from its frozen preflight receipt')
    for language in ('C', 'CXX'):
        key = 'CMAKE_' + language + '_COMPILER_AR'
        if values.get(key):
            if not any(_same_path(values[key], tools[tool]) for tool in ('llvm_ar', 'llvm_lib')):
                raise LLVMError(f'{key}: compiler-specific archiver is not verified LLVM')
            selected[key] = executable_receipt(values[key], name=Path(values[key]).name,
                                               provenance='actual CMake compiler-specific archiver')
            if type(tools) is VerifiedTools:
                tool = 'llvm_ar' if _same_path(values[key], tools['llvm_ar']) else 'llvm_lib'
                if selected[key]['sha256'] != tools.receipts[tool]['sha256']:
                    raise LLVMError(f'{key}: archiver differs from its frozen preflight receipt')
    languages = {}
    for language, identity in identities.items():
        argument = re.findall(r'set\(CMAKE_' + language + r'_COMPILER_ARG1 "([^"]*)"\)', identity)
        if argument != ['-fuse-ld=lld']:
            raise LLVMError(f'CMake {language} compiler identification lacks the required single LLD driver selector')
        wanted = {'COMPILER_ID': 'Clang', 'SIMULATE_ID': 'MSVC', 'COMPILER_FRONTEND_VARIANT': 'MSVC'}
        for suffix, value in wanted.items():
            if re.findall(r'set\(CMAKE_' + language + '_' + suffix + r' "([^"]*)"\)', identity) != [value]:
                raise LLVMError(f'CMake {language} identity is not Clang with the MSVC ABI/frontend')
        linker_paths = re.findall(r'set\(CMAKE_' + language + r'_COMPILER_LINKER "([^"]*)"\)', identity)
        linker_ids = re.findall(r'set\(CMAKE_' + language + r'_COMPILER_LINKER_ID "([^"]*)"\)', identity)
        if len(linker_paths) != 1 or not _same_path(linker_paths[0], tools['lld_link']) or linker_ids != ['LLD']:
            raise LLVMError(f'CMake {language} actual compiler linker is not the verified LLD')
        languages[language] = {'id': 'Clang', 'simulate_id': 'MSVC', 'frontend_variant': 'MSVC',
                               'compiler': values['CMAKE_' + language + '_COMPILER'], 'compiler_arg1': argument[0],
                               'linker': linker_paths[0], 'linker_id': 'LLD'}
    # Record other compiler/assembler and SDK helper selections without copying
    # flags, cache contents, or environment values into diagnostics.
    for key, value in values.items():
        if key in selected or not value or not re.fullmatch(r'CMAKE_(?:[A-Z0-9_]+_COMPILER|MT|LINKER|AR)', key):
            continue
        if key.endswith('_COMPILER') and ('-' in key or key.endswith('_COMPILER_ID')):
            continue
        if Path(value).name.lower() in ('cl.exe', 'lib.exe', 'link.exe'):
            raise LLVMError(f'{key}: Microsoft compiler/library/linker fallback is forbidden')
        if key in ('CMAKE_RC_COMPILER', 'CMAKE_MT'):
            normalized = str(value).replace('\\', '/').lower()
            name = Path(value).name.lower()
            llvm_helper = (key, name) in (('CMAKE_MT', 'llvm-mt.exe'), ('CMAKE_RC_COMPILER', 'llvm-rc.exe'))
            if llvm_helper:
                if not _same_path(str(Path(value).parent), str(Path(tools['clang_cl']).parent)):
                    raise LLVMError(f'{key}: LLVM helper is not a selected compiler sibling')
            elif '/windows kits/' not in normalized or name != ('rc.exe' if key == 'CMAKE_RC_COMPILER' else 'mt.exe'):
                raise LLVMError(f'{key}: expected an existing Windows SDK helper or verified LLVM sibling')
            selected[key] = executable_receipt(value, name=name, provenance='actual CMake SDK/LLVM helper')
            selected[key]['version'] = _windows_file_version(value)
            if llvm_helper:
                if selected[key]['version'] != LLVM_VERSION + '.0':
                    raise LLVMError(f'{key}: LLVM helper version does not match selected LLVM')
                if type(tools) is VerifiedTools and not preflight:
                    baseline = tools.receipts.get('llvm_mt' if key == 'CMAKE_MT' else 'llvm_rc')
                    if baseline is None or selected[key]['sha256'] != baseline['sha256']:
                        raise LLVMError(f'{key}: LLVM helper lacks matching frozen preflight provenance')
        else:
            selected[key] = _receipt(value, provenance='actual CMake auxiliary compiler/assembler selection')
            if key.startswith('CMAKE_ASM') and key.endswith('_COMPILER'):
                known = next((tool for tool in ('clang_cl', 'nasm') if tool in tools and _same_path(value, tools[tool])), None)
                if known is None:
                    raise LLVMError(f'{key}: actual assembler has no verified preflight provenance')
                if type(tools) is VerifiedTools and selected[key]['sha256'] != tools.receipts[known]['sha256']:
                    raise LLVMError(f'{key}: assembler differs from its frozen preflight receipt')
    return {'cache': str((build / 'CMakeCache.txt').resolve()), 'generator': 'Ninja',
            'toolchain': toolchain, 'linker_type': 'LLD', 'languages': languages, 'tools': selected}


def _write_json(path, value):
    content = json.dumps(value, indent=2, ensure_ascii=False) + '\n'
    if len(content.encode('utf-8')) > 512 * 1024:
        raise LLVMError('LLVM evidence exceeded its report byte bound')
    Path(path).write_text(content, encoding='utf-8')


def _save_evidence(path, report):
    original_failure = sys.exc_info()[1]
    try:
        _write_json(path, report)
    except Exception as error:
        if original_failure is None:
            raise
        print(f'[windows-llvm] evidence write failed: {type(error).__name__}', flush=True)


def _version(output, pattern, label):
    if len(output.encode('utf-8')) > TEXT_LIMIT:
        raise LLVMError(f'{label}: version output exceeded its byte bound')
    match = re.search(pattern, output, re.M)
    if match is None:
        raise LLVMError(f'{label}: version/target does not match the verified candidate')
    return match.group(0).strip()


def _primary_zero_output(output):
    # run_command's original Windows supervisor appends this exact trailer.
    # It is metadata after ownership/status acceptance, not an exit-status waiver.
    trailer = '[release-child-exit] decimal=0 hex=0x00000000\n'
    if not output.endswith(trailer) or output.count('[release-child-exit]') != 1:
        raise LLVMError('owned path probe lacks one exact zero-status supervisor trailer')
    return output[:-len(trailer)]


def _collect_cmake_failure(build):
    """Read fixed owned CMake diagnostics; export selections, never commands."""
    build = Path(build)
    report = {'status': 'collected', 'files': [], 'observations': [], 'input_bytes': 0,
              'directory_limit_reached': False, 'file_limit_reached': False,
              'record_limit_reached': False}
    path_keys = {
        'CMAKE_C_COMPILER': ('clang-cl.exe', 'cl.exe', 'clang.exe', 'gcc.exe'),
        'CMAKE_CXX_COMPILER': ('clang-cl.exe', 'cl.exe', 'clang++.exe', 'g++.exe'),
        'CMAKE_COMPILER': ('clang-cl.exe', 'cl.exe', 'clang.exe', 'clang++.exe'),
        'CMAKE_AR': ('llvm-lib.exe', 'llvm-ar.exe', 'lib.exe'),
        'CMAKE_LINKER': ('lld-link.exe', 'link.exe'),
        'CMAKE_LINKER_LLD': ('lld-link.exe', 'link.exe'),
        'CMAKE_MAKE_PROGRAM': ('ninja.exe', 'nmake.exe'),
        'CMAKE_COMMAND': ('cmake.exe',),
        'CMAKE_RC_COMPILER': ('rc.exe', 'llvm-rc.exe'),
        'CMAKE_MT': ('mt.exe', 'llvm-mt.exe'),
    }
    for language in ('C', 'CXX'):
        path_keys['CMAKE_' + language + '_COMPILER_LINKER'] = ('lld-link.exe', 'link.exe')
        path_keys['CMAKE_' + language + '_COMPILER_AR'] = ('llvm-ar.exe', 'llvm-lib.exe', 'lib.exe')
    seen, record_bytes = set(), 0

    def observe(source, key, value):
        nonlocal record_bytes
        row = {'source': source, 'key': key}
        if key in path_keys:
            if (not ntpath.isabs(value) or ntpath.basename(value).lower() not in path_keys[key] or
                    any(token in value for token in (';', '"', '\r', '\n', '\0'))):
                return
            row.update(value=value[:1024], value_truncated=len(value) > 1024)
        elif re.fullmatch(r'CMAKE_(?:(?:C|CXX)_)?COMPILER_ARG1', key):
            arguments = value.split()
            selectors = [token for token in arguments
                         if token in ('-fuse-ld=lld', '-fuse-ld=lld-link', '-fuse-ld=link',
                                      '-fuse-ld=bfd', '-fuse-ld=gold', '-fuse-ld=mold')]
            row.update(linker_selectors=selectors[:16], other_argument_count=len(arguments) - len(selectors))
        elif (key in ('CMAKE_GENERATOR', 'CMAKE_LINKER_TYPE') or
              re.fullmatch(r'CMAKE_(?:C|CXX)_(?:COMPILER_ID|SIMULATE_ID|COMPILER_FRONTEND_VARIANT|COMPILER_LINKER_ID)', key)):
            if not re.fullmatch(r'[A-Za-z0-9_ .+-]{1,64}', value):
                return
            row['value'] = value
        elif re.fullmatch(r'CMAKE_(?:(?:C|CXX)_COMPILER|CACHE_(?:MAJOR|MINOR|PATCH))_VERSION', key):
            if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,3}', value) or len(value) > 32:
                return
            row['value'] = value
        else:
            return
        encoded = json.dumps(row, sort_keys=True)
        if encoded in seen:
            return
        if len(report['observations']) >= 64 or record_bytes + len(encoded.encode('utf-8')) > 48 * 1024:
            report['record_limit_reached'] = True
            return
        seen.add(encoded)
        record_bytes += len(encoded.encode('utf-8'))
        report['observations'].append(row)

    def read(path, *, configure_log=False):
        if path.is_symlink():
            return
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise LLVMError('CMake failure diagnostic is not a regular file')
        if len(report['files']) >= 18 or report['input_bytes'] >= 512 * 1024:
            report['file_limit_reached'] = True
            return
        limit = min(TEXT_LIMIT, 512 * 1024 - report['input_bytes'])
        with path.open('rb') as stream:
            data = stream.read(limit)
        source = path.relative_to(build).as_posix()
        truncated = metadata.st_size > len(data)
        report['files'].append({'source': source, 'bytes_read': len(data), 'truncated': truncated})
        report['input_bytes'] += len(data)
        lines = data.decode('utf-8', errors='replace').splitlines()
        if truncated and not data.endswith(b'\n'):
            lines = lines[:-1]
        language = None
        for line in lines:
            if configure_log:
                identified = re.search(r'Compiling the (C|CXX) compiler identification source file ', line)
                if identified:
                    language = identified.group(1)
                compiler = re.fullmatch(r'\s*Compiler: (.*?\.exe)(?: (.*))?', line)
                if compiler:
                    prefix = 'CMAKE_' + (language + '_' if language else '') + 'COMPILER'
                    observe(source, prefix, compiler.group(1))
                    observe(source, prefix + '_ARG1', compiler.group(2) or '')
            else:
                selection = re.fullmatch(r'(CMAKE_[A-Z0-9_]+):[^=]+=(.*)', line)
                if selection is None:
                    selection = re.fullmatch(r'set\((CMAKE_[A-Z0-9_]+) "([^"]*)"\)', line)
                if selection:
                    observe(source, *selection.groups())

    read(build / 'CMakeCache.txt')
    files = build / 'CMakeFiles'
    if files.is_symlink() or not files.is_dir():
        return report
    read(files / 'CMakeConfigureLog.yaml', configure_log=True)
    candidates = []
    with os.scandir(files) as entries:
        for index, entry in enumerate(entries):
            path = Path(entry.path)
            if (not path.is_symlink() and entry.is_dir(follow_symlinks=False) and
                    re.fullmatch(r'[A-Za-z0-9_.+-]{1,80}', entry.name)):
                candidates.append(path)
            if index == 63:
                report['directory_limit_reached'] = True
                break
    if len(candidates) > 8:
        report['file_limit_reached'] = True
    for path in sorted(candidates)[:8]:
        for language in ('C', 'CXX'):
            read(path / ('CMake' + language + 'Compiler.cmake'))
    return report


def preflight(directory, environment, *, run_command, timeout, shutdown_timeout):
    """Verify existing tools and actual SDK build products under original ownership."""
    directory = Path(directory)
    report = {'successful': False, 'native_validation': 'incomplete', 'tools': {}, 'probes': {},
              'provenance': PROVENANCE}
    tools = {}
    cmake_started = False
    root = directory / 'modern-windows-llvm'
    try:
        if sys.platform != 'win32':
            raise LLVMError('Windows LLVM preflight requires Windows')
        root.mkdir(exist_ok=True)
        def existing(key, name, *, sibling=None, provenance='existing child PATH', search_path=None):
            path = str(Path(sibling) / name) if sibling else shutil.which(name, path=environment.get('PATH') if search_path is None else search_path)
            if path is None:
                raise LLVMError(f'{name}: required existing tool is missing; no installation or fallback')
            try:
                receipt = executable_receipt(path, name=name, provenance=provenance)
            except LLVMError as error:
                if error.rejected_receipt is not None:
                    report.setdefault('rejected_tools', {})[key] = error.rejected_receipt
                rejected = error.rejected_receipt or {}
                if (key != 'ninja' or rejected.get('sha256') != NINJA_SHIM_SHA256 or
                        rejected.get('bytes') != 392704 or rejected.get('dos_signature_hex') != '4d5a' or
                        rejected.get('pe_offset') != 128 or rejected.get('pe_signature_hex') != '50450000' or
                        rejected.get('machine_code') != 0x014c or rejected.get('path_truncated')):
                    raise
                resolution = {'status': 'checking_pinned_package'}
                report['ninja_resolution'] = resolution
                try:
                    receipt = _resolve_chocolatey_ninja(path, environment, resolution)
                except LLVMError as failure:
                    resolution.update(status='failed', error=str(failure)[:1024])
                    if failure.rejected_receipt is not None:
                        resolution['rejected_target'] = failure.rejected_receipt
                    raise
            tools[key] = receipt['path']
            report['tools'][key] = receipt
        for key, name in (('clang_cl', 'clang-cl.exe'), ('cmake', 'cmake.exe'),
                          ('ninja', 'ninja.exe'), ('rustup', 'rustup.exe')):
            existing(key, name)
        for key, name in (('llvm_lib', 'llvm-lib.exe'), ('llvm_ar', 'llvm-ar.exe'), ('lld_link', 'lld-link.exe')):
            existing(key, name, sibling=Path(tools['clang_cl']).parent,
                     provenance='existing sibling of selected clang-cl')
        if report['tools']['llvm_lib']['sha256'] != report['tools']['llvm_ar']['sha256']:
            raise LLVMError('llvm-lib/llvm-ar alias bytes differ; LLVM 20.1.8 version provenance is unproved')
        def run(suffix, command, *, child=None, **private):
            return run_command(command, name='modern-windows-llvm-' + suffix,
                environment=(environment if child is None else child).copy(), directory=directory,
                timeout=timeout, shutdown_timeout=shutdown_timeout, **private)
        path_output = _primary_zero_output(run('rustc-path', [tools['rustup'], 'which', '--toolchain', '1.99.1-' + TARGET, 'rustc']))
        if (len(path_output.splitlines()) != 1 or not Path(path_output.strip()).is_absolute() or
                Path(path_output.strip()).name.lower() != 'rustc.exe'):
            raise LLVMError('pinned Rust compiler path is absent or ambiguous')
        existing('rustc', 'rustc.exe', sibling=Path(path_output.strip()).parent,
                 provenance='rustup which --toolchain 1.99.1-' + TARGET)
        rust_version = run('rustc-version', [tools['rustc'], '--version', '--verbose'])
        _version(rust_version, r'^rustc 1\.99\.1(?:\s|$)', 'rustc')
        _version(rust_version, r'^host: x86_64-pc-windows-msvc\s*$', 'rustc host')
        _version(rust_version, r'^release: 1\.99\.1\s*$', 'rustc release')
        report['tools']['rustc']['version'] = '1.99.1'
        sysroot_output = _primary_zero_output(run('rustc-sysroot', [tools['rustc'], '--print', 'sysroot']))
        if len(sysroot_output.splitlines()) != 1:
            raise LLVMError('pinned Rust sysroot path is absent or ambiguous')
        sysroot = sysroot_output.strip()
        if not _same_path(sysroot, str(Path(tools['rustc']).parent.parent)):
            raise LLVMError('Rust compiler/sysroot paths disagree')
        existing('rust_lld', 'rust-lld.exe', sibling=Path(sysroot) / 'lib/rustlib' / TARGET / 'bin',
                 provenance='bundled linker in verified Rust 1.99.1 sysroot')
        versions = (
            ('clang_cl', 'clang-version', ['--version'], r'^clang version 20\.1\.8(?:\s|$)'),
            ('llvm_ar', 'archive-version', ['--version'], r'^\s*LLVM version 20\.1\.8(?:\s|$)'),
            ('lld_link', 'linker-version', ['--version'], r'^LLD 20\.1\.8(?:\s|$)'),
            ('rust_lld', 'rust-linker-version', ['-flavor', 'link', '--version'], r'^LLD \d+\.\d+\.\d+(?:\s|$)'),
            ('cmake', 'cmake-version', ['--version'], r'^cmake version 4\.4\.5\s*$'),
            ('ninja', 'ninja-version', ['--version'], r'^1\.13\.2\s*$'),
        )
        for key, suffix, args, pattern in versions:
            output = run(suffix, [tools[key], *args])
            report['tools'][key]['version'] = _version(output, pattern, key)
            if key == 'clang_cl':
                _version(output, r'^Target: x86_64-pc-windows-msvc\s*$', 'clang-cl target')
        report['tools']['llvm_lib'].update(version=LLVM_VERSION,
            version_source='identical llvm-ar executable bytes',
            version_source_sha256=report['tools']['llvm_ar']['sha256'])
        report['tools']['cmake_toolchain'] = _prepare_toolchain(root, tools)
        child = environment_for(environment, platform='win32', modern=True, opt_in=True,
                                check_name='modern-rust-clippy', tools=tools)
        nasm = shutil.which('nasm.exe', path=child['PATH'])
        assembler = {'status': 'nasm_absent_prebuilt_pending', 'locked_crate': 'aws-lc-sys 0.47.0',
                     'crate_sha256': AWS_LC_CRATE_SHA256, 'actual_selection': 'pending Cargo output/object evidence'}
        if nasm is not None:
            existing('nasm', 'nasm.exe', search_path=child['PATH'],
                     provenance='aws-lc-sys 0.47.0 direct NasmBuilder command resolved from exact Cargo child PATH')
            report['tools']['nasm']['version'] = _version(run('nasm-version', [tools['nasm'], '-v'], child=child),
                r'^NASM version \d+\.\d+(?:\.\d+)?(?:\s|$)', 'nasm')
            assembler = {'status': 'existing_nasm_verified'}
        else:
            lock = _bounded(Path(__file__).resolve().parents[2] / 'Cargo.lock', ARTIFACT_LIMIT).decode('utf-8')
            if not re.search(r'name = "aws-lc-sys"\nversion = "0\.47\.0"\n[^\n]*\nchecksum = "' + AWS_LC_CRATE_SHA256 + '"', lock):
                raise LLVMError('prebuilt assembly manifest is not bound to the current locked AWS-LC package')
        report['assembler'] = assembler
        selected_tools = VerifiedTools(tools, report['tools'], assembler)
        verify_tools(selected_tools)
        if nasm is not None:
            assembly, asm_obj = root / 'positive.asm', root / 'nasm-positive.obj'
            if asm_obj.exists():
                raise LLVMError('NASM preflight output already exists')
            assembly.write_bytes(b'BITS 64\nsection .text\nglobal ori_llvm_asm\nori_llvm_asm:\n    ret\n')
            output = run('nasm-positive', [tools['nasm'], '-f', 'win64', '-o', str(asm_obj), str(assembly)], child=child)
            report['probes']['assembler'] = validate_probe(0, output, asm_obj, negative=False)
        source, obj, archive = root / 'positive.c', root / 'positive.obj', root / 'positive.lib'
        if obj.exists() or archive.exists():
            raise LLVMError('preflight build products already exist')
        source.write_text('int ori_llvm_positive(void) { return 42; }\n', encoding='ascii')
        output = run('positive', [tools['clang_cl'], '/nologo', '/c', '/MD', '/Fo' + str(obj), str(source)], child=child)
        report['probes']['positive'] = validate_probe(0, output, obj, negative=False)
        negative = prepare_negative_probe(directory, tools)
        run('negative', negative.command, child=child, _llvm_negative_probe=negative)
        # The private runner path has already checked status 1 and the original
        # Job/JOIN gates; no exception or parsed log is reinterpreted here.
        report['probes']['negative'] = {'status': 'expected_compiler_error', 'exit_code': 1,
                                        'marker': NEGATIVE_MARKER, 'object_created': False}
        run('archive', [tools['llvm_lib'], '/NOLOGO', '/OUT:' + str(archive), str(obj)], child=child)
        report['probes']['archive'] = validate_archive(archive, obj)
        project, build = root / 'cmake', root / 'cmake-build'
        project.mkdir(exist_ok=True)
        (project / 'CMakeLists.txt').write_text(
            'cmake_minimum_required(VERSION 4.4)\nproject(ORIWindowsLLVM LANGUAGES C CXX)\n'
            'add_library(ori_llvm_probe_lib STATIC positive.c)\n'
            'set_target_properties(ori_llvm_probe_lib PROPERTIES OUTPUT_NAME ori_llvm_probe)\n'
            'add_executable(ori_llvm_probe main.cpp)\n'
            'target_link_libraries(ori_llvm_probe PRIVATE ori_llvm_probe_lib kernel32)\n', encoding='ascii')
        (project / 'positive.c').write_text(
            '#include <windows.h>\nunsigned long ori_llvm_sdk(void) { return GetCurrentProcessId(); }\n', encoding='ascii')
        (project / 'main.cpp').write_text(
            'extern "C" unsigned long ori_llvm_sdk(void);\nint main() { return ori_llvm_sdk() == 0; }\n', encoding='ascii')
        configure = [tools['cmake'], '-S', str(project), '-B', str(build), '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Debug',
                     '-DCMAKE_TOOLCHAIN_FILE=' + tools['cmake_toolchain'].replace('\\', '/')]
        cmake_started = True
        run('cmake-configure', configure, child=child)
        report['probes']['cmake'] = validate_cmake(build, selected_tools, preflight=True)
        for key, tool in (('CMAKE_RC_COMPILER', 'llvm_rc'), ('CMAKE_MT', 'llvm_mt')):
            actual = report['probes']['cmake']['tools'].get(key)
            if actual and Path(actual['path']).name.lower() in ('llvm-rc.exe', 'llvm-mt.exe'):
                tools[tool] = actual['path']
                report['tools'][tool] = dict(actual)
        selected_tools = VerifiedTools(tools, report['tools'], assembler)
        if set(report['probes']['cmake']['languages']) != {'C', 'CXX'}:
            raise LLVMError('CMake SDK preflight did not configure both C and C++')
        run('cmake-build', [tools['cmake'], '--build', str(build), '--parallel', '2'], child=child)
        report['probes']['cmake']['executable'] = executable_receipt(build / 'ori_llvm_probe.exe',
            name='ori_llvm_probe.exe', provenance='actual CMake/Ninja Windows SDK C/C++ link')
        report['probes']['cmake']['library'] = validate_archive(build / 'ori_llvm_probe.lib')
        if any(report['probes']['cmake'][key]['bytes'] > ARTIFACT_LIMIT for key in ('executable', 'library')):
            raise LLVMError('CMake probe products exceeded their artifact byte bounds')
        verify_tools(selected_tools)
        report.update(successful=True, native_validation='preflight_passed_cargo_not_yet_validated')
        return selected_tools
    except BaseException as error:
        report['successful'] = False
        report['error_type'] = type(error).__name__
        if isinstance(error, LLVMError):
            report['error'] = str(error)
        if cmake_started:
            try:
                report['cmake_failure'] = _collect_cmake_failure(root / 'cmake-build')
            except BaseException as collection_error:
                report['cmake_failure'] = {'status': 'collection_failed', 'error_type': type(collection_error).__name__}
        raise
    finally:
        _save_evidence(directory / 'modern-windows-llvm.json', report)


def _prebuilt_assembler_evidence(builds):
    if not builds or len(builds) > 8:
        raise LLVMError('NASM absent: actual AWS-LC prebuilt selection is missing or ambiguous')
    result = []
    for build in builds:
        output = _bounded(build / 'output', 512 * 1024).decode('utf-8', errors='strict')
        lines = output.splitlines()
        required = {'cargo:warning=Building with: CC',
                    "cargo:warning=Target platform: 'x86_64-pc-windows-msvc'",
                    'cargo:warning=NASM command not found or failed to execute.'}
        if not required.issubset(lines):
            raise LLVMError('NASM absent: actual Cargo build output does not establish the locked CC prebuilt path')
        out = build / 'out'
        libraries = [line.removeprefix('cargo:libdir=') for line in lines if line.startswith('cargo:libdir=')]
        if out.is_symlink() or len(libraries) != 1 or not _same_path(libraries[0], str(out)):
            raise LLVMError('AWS-LC completed-build metadata does not identify its original owned output directory')
        objects = []
        for name, expected_sha256 in sorted(PREBUILT_NASM.items()):
            path = out / name
            if path.is_symlink():
                raise LLVMError('AWS-LC prebuilt object is not an original owned file')
            _coff(_bounded(path, ARTIFACT_LIMIT))
            actual = _receipt(path, provenance='aws-lc-sys 0.47.0 locked builder/prebuilt-nasm/' + name)
            if actual['sha256'] != expected_sha256:
                raise LLVMError('AWS-LC output does not match the locked prebuilt assembly object')
            objects.append(actual)
        if not objects:
            raise LLVMError('AWS-LC prebuilt assembly manifest has no objects')
        result.append({'output': _receipt(build / 'output', provenance='actual Cargo build-script output; content not retained'),
                       'markers': sorted(required), 'objects': objects})
    return {'status': 'locked_prebuilt_objects_verified', 'crate': 'aws-lc-sys 0.47.0',
            'crate_sha256': AWS_LC_CRATE_SHA256, 'builds': result}


def audit_cargo(directory, target, tools, *, check_name, environment=None, command_succeeded=True):
    """Audit actual fresh CMake caches after a Cargo command, with bounded walks."""
    path = Path(directory) / ('modern-windows-llvm-' + check_name + '-cmake.json')
    report = {'check': check_name, 'successful': False, 'cache_count': 0, 'caches': []}
    def unreadable(error):
        raise error
    try:
        verify_tools(tools, environment=environment)
        if not Path(target).is_dir():
            raise LLVMError('Cargo target directory is unavailable for the CMake audit')
        visited, assembly_builds = 0, []
        for parent, directories, files in os.walk(target, followlinks=False, onerror=unreadable):
            visited += 1
            if visited > 20000:
                raise LLVMError('Cargo CMake cache directory bound exceeded')
            directories[:] = sorted(name for name in directories if not (Path(parent) / name).is_symlink())
            build = Path(parent)
            if build.name.startswith('aws-lc-sys-') and build.parent.name == 'build' and 'output' in files:
                if len(assembly_builds) >= 8:
                    raise LLVMError('AWS-LC build-output count bound exceeded')
                assembly_builds.append(build)
            if 'CMakeCache.txt' in files:
                if len(report['caches']) >= CACHE_LIMIT:
                    raise LLVMError('Cargo CMake cache count bound exceeded')
                report['caches'].append(validate_cmake(Path(parent), tools))
        if 'nasm' in tools:
            report['direct_build_assembler'] = dict(tools.receipts['nasm'])
        elif tools.assembler.get('status') == 'nasm_absent_prebuilt_pending':
            if not command_succeeded:
                raise LLVMError('NASM absent: prebuilt selection remains incomplete after an owned-command failure')
            report['assembler'] = _prebuilt_assembler_evidence(assembly_builds)
        report['provenance_scope'] = 'verified selected tools and CMake caches; not an all-subprocess trace'
        verify_tools(tools, environment=environment)
        report.update(successful=True, cache_count=len(report['caches']))
    except BaseException as error:
        report['successful'] = False
        report['error_type'] = type(error).__name__
        if isinstance(error, LLVMError):
            report['error'] = str(error)
        raise
    finally:
        _save_evidence(path, report)


def retain_artifacts(directory, destination):
    source = Path(directory) / 'modern-windows-llvm'
    if not source.is_dir():
        return
    target = Path(destination) / 'modern-windows-llvm'
    target.mkdir()
    report, total = {}, 0
    for name in ARTIFACTS:
        path = source / name
        if not path.is_file() or path.is_symlink():
            continue
        size = path.stat().st_size
        if size > ARTIFACT_LIMIT or total + size > ARTIFACT_LIMIT * 4:
            report[name] = {'status': 'too_large', 'bytes': size}
            continue
        data = _bounded(path, ARTIFACT_LIMIT)
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        total += len(data)
        report[name] = {'status': 'retained', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    for suffix in LOG_SUFFIXES:
        name = 'modern-windows-llvm-' + suffix + '.log'
        path = Path(directory) / name
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as log:
            data = log.read(TEXT_LIMIT)
        (Path(destination) / name).write_bytes(data)
        size = path.stat().st_size
        report[name] = {'status': 'truncated' if size > len(data) else 'retained',
                        'original_bytes': size, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    _write_json(target / 'retention.json', report)
