"""Pure LLVM selection/outcome checks. No SDK, compiler, subprocess or install."""
import copy
from contextlib import redirect_stdout, redirect_stderr
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import run_release_checks as release
from tools.modernization import windows_llvm as llvm


def pe_x64():
    data = bytearray(128)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 64)
    data[64:68] = b'PE\0\0'
    struct.pack_into('<H', data, 68, 0x8664)
    return bytes(data)


def coff_x64():
    # One .text section containing RET; literal COFF layout, no compiler needed.
    return (struct.pack('<HHIIIHH', 0x8664, 1, 0, 0, 0, 0, 0) + b'.text\0\0\0' +
            struct.pack('<IIIIIIHHI', 0, 0, 1, 60, 0, 0, 0, 0, 0x60000020) + b'\xc3')


def archive_member(payload):
    header = b'positive.obj/   ' + b'0           ' + b'0     ' + b'0     ' + b'100644  '
    header += str(len(payload)).encode().ljust(10) + b'`\n'
    return b'!<arch>\n' + header + payload + (b'\n' if len(payload) % 2 else b'')


class WindowsLLVMTests(unittest.TestCase):
    def setUp(self):
        self.parent = {'PATH': 'original-tool-path', 'INCLUDE': 'same-sdk', 'LIB': 'same-libs',
                       'OTHER_SECRET': 'do-not-report'}
        self.tools = {'clang_cl': '/verified/LLVM/bin/clang-cl.exe',
                      'llvm_lib': '/verified/LLVM/bin/llvm-lib.exe',
                      'rust_lld': '/verified/Rust1.99.1/bin/rust-lld.exe'}

    def env(self, **overrides):
        args = dict(platform='win32', modern=True, opt_in=True,
                    check_name='modern-rust-clippy', tools=self.tools)
        args.update(overrides)
        return llvm.environment_for(self.parent, **args)

    def test_explicit_windows_modern_cargo_selects_verified_tools_in_a_copy(self):
        before = copy.deepcopy(self.parent)
        actual = self.env()
        self.assertEqual(actual.get('CC_x86_64_pc_windows_msvc'), '/verified/LLVM/bin/clang-cl.exe')
        self.assertEqual(actual.get('CXX_x86_64_pc_windows_msvc'), '/verified/LLVM/bin/clang-cl.exe')
        self.assertEqual(actual.get('AR_x86_64_pc_windows_msvc'), '/verified/LLVM/bin/llvm-lib.exe')
        self.assertEqual(actual.get('CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER'), '/verified/Rust1.99.1/bin/rust-lld.exe')
        self.assertEqual(actual.get('CMAKE_GENERATOR'), 'Ninja')
        self.assertEqual(actual['PATH'], '/verified/LLVM/bin;original-tool-path')
        self.assertEqual(set(actual) - set(self.parent), {
            'CC_x86_64_pc_windows_msvc', 'CXX_x86_64_pc_windows_msvc',
            'AR_x86_64_pc_windows_msvc', 'CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER',
            'CMAKE_GENERATOR'})
        self.assertEqual(actual['INCLUDE'], 'same-sdk')
        self.assertEqual(actual['LIB'], 'same-libs')
        self.assertEqual(self.parent, before)
        self.assertIsNot(actual, self.parent)

    def test_all_default_other_os_non_cargo_app_and_package_environments_stay_exact(self):
        for change in ({'opt_in': False}, {'platform': 'linux'}, {'platform': 'darwin'},
                       {'modern': False}, {'check_name': 'modern-rust-format'},
                       {'check_name': 'modern-cargo'}, {'check_name': 'modern-scientific-http'},
                       {'check_name': 'modern-core-client-interop'}, {'check_name': 'legacy-package'}):
            with self.subTest(change=change):
                actual = self.env(**change, **({'tools': None} if change else {}))
                self.assertEqual(actual, self.parent)
                self.assertIsNot(actual, self.parent)

    def test_inherited_higher_priority_compiler_and_cmake_selectors_fail_closed_only_in_selected_child(self):
        self.tools['cmake'] = '/verified/CMake/bin/cmake.exe'
        conflicts = {'CC_x86_64-pc-windows-msvc': 'C:/VS/cl.exe',
                     'CC_X86_64-PC-WINDOWS-MSVC': 'C:/VS/cl.exe',
                     'CXX_x86_64-pc-windows-msvc': 'C:/VS/cl.exe',
                     'AR_x86_64-pc-windows-msvc': 'C:/VS/lib.exe',
                     'CMAKE_GENERATOR_x86_64-pc-windows-msvc': 'Visual Studio 18 2026',
                     'CMAKE_GENERATOR_x86_64_pc_windows_msvc': 'Visual Studio 18 2026',
                     'HOST_CMAKE_GENERATOR': 'Visual Studio 18 2026',
                     'CMAKE_x86_64-pc-windows-msvc': 'C:/other/cmake.exe',
                     'CMAKE_x86_64_pc_windows_msvc': 'C:/other/cmake.exe',
                     'HOST_CMAKE': 'C:/other/cmake.exe', 'CMAKE': 'C:/other/cmake.exe',
                     'AWS_LC_SYS_CC': 'C:/VS/cl.exe', 'AWS_LC_SYS_CXX_x86_64_pc_windows_msvc': 'C:/VS/cl.exe',
                     'AWS_LC_SYS_HOST_CC': 'C:/VS/cl.exe', 'HOST_CXX': 'C:/VS/cl.exe',
                     'AWS_LC_SYS_CMAKE_GENERATOR': 'Visual Studio 18 2026',
                     'AWS_LC_SYS_CMAKE': 'C:/other/cmake.exe'}
        conflicts.update(RUSTC='C:/other/rustc.exe', CARGO_BUILD_TARGET='aarch64-pc-windows-msvc')
        for key, value in conflicts.items():
            with self.subTest(key=key):
                original = dict(self.parent, **{key: value})
                self.parent = original.copy()
                with self.assertRaises(llvm.LLVMError): self.env()
                self.assertEqual(self.parent, original)
                self.assertEqual(self.env(opt_in=False, tools=None), original)
                self.assertEqual(self.env(check_name='modern-core-client-interop', tools=None), original)
                self.parent.pop(key)
        self.parent.update({'CC_x86_64-pc-windows-msvc': self.tools['clang_cl'],
                            'AR_x86_64-pc-windows-msvc': self.tools['llvm_lib'],
                            'CMAKE_GENERATOR_x86_64_pc_windows_msvc': 'Ninja',
                            'CMAKE': self.tools['cmake']})
        self.assertEqual(self.env()['CC_x86_64_pc_windows_msvc'], self.tools['clang_cl'])

    def test_windows_case_aliases_do_not_leave_two_child_values_for_selected_keys(self):
        self.parent['CC_X86_64_PC_WINDOWS_MSVC'] = 'C:/old/cl.exe'
        self.parent['CXX_X86_64_PC_WINDOWS_MSVC'] = 'C:/old/cl.exe'
        self.parent['AR_X86_64_PC_WINDOWS_MSVC'] = 'C:/old/lib.exe'
        before = self.parent.copy()
        child = self.env()
        for key, value in (('cc_x86_64_pc_windows_msvc', self.tools['clang_cl']),
                           ('cxx_x86_64_pc_windows_msvc', self.tools['clang_cl']),
                           ('ar_x86_64_pc_windows_msvc', self.tools['llvm_lib'])):
            self.assertEqual([v for k, v in child.items() if k.casefold() == key], [value])
        self.assertEqual(self.parent, before)

    def test_public_opt_in_rejects_non_windows_and_non_modern_before_running(self):
        for platform, args in (('linux', ['--modern', '--windows-llvm']),
                               ('darwin', ['--modern', '--windows-llvm']),
                               ('win32', ['--windows-llvm'])):
            with self.subTest(platform=platform, args=args), \
                    patch.object(release.sys, 'platform', platform), \
                    patch.object(release, 'release_checks') as run, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    release.main(args)
                self.assertEqual(caught.exception.code, 2)
                run.assert_not_called()
        with patch.object(release.sys, 'platform', 'win32'), \
                patch.object(release, 'release_checks') as run:
            self.assertEqual(release.main(['--modern', '--windows-llvm', '--research-python', 'prepared']), 0)
        self.assertTrue(run.call_args.kwargs['windows_llvm'])

    def test_programmatic_invalid_opt_in_fails_before_creating_a_scope(self):
        for platform, modern in (('linux', True), ('darwin', True), ('win32', False)):
            with self.subTest(platform=platform, modern=modern), \
                    patch.object(release.sys, 'platform', platform), \
                    patch.object(release, '_release_temporary_directory') as scope:
                with self.assertRaises(release.ReleaseCheckError):
                    release.release_checks(modern=modern, windows_llvm=True)
                scope.assert_not_called()

    def test_negative_requires_exact_one_error_marker_and_no_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            obj = Path(temporary) / 'negative.obj'
            output = 'negative.c(1,2): error: ' + llvm.NEGATIVE_MARKER + '\n'
            error = None
            result = None
            try:
                result = llvm.validate_probe(1, output, obj, negative=True)
            except llvm.LLVMError as caught:
                error = str(caught)
            self.assertIsNone(error, 'literal expected diagnostic must be distinguished from a gate failure')
            self.assertEqual(result['status'], 'expected_compiler_error')
            for status, text in ((0, output), (2, output), (0xC0000005, output), (1, 'other error'), (True, output)):
                with self.subTest(status=status, text=text), self.assertRaises(llvm.LLVMError):
                    llvm.validate_probe(status, text, obj, negative=True)
            for text in ('#error ' + llvm.NEGATIVE_MARKER, output.rstrip() + '_OTHER\n'):
                with self.subTest(text=text), self.assertRaises(llvm.LLVMError):
                    llvm.validate_probe(1, text, obj, negative=True)
            obj.write_bytes(b'unexpected object')
            with self.assertRaises(llvm.LLVMError):
                llvm.validate_probe(1, output, obj, negative=True)

    def test_positive_requires_actual_x64_coff_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            obj = Path(temporary) / 'positive.obj'
            obj.write_bytes(coff_x64())
            error = None
            result = None
            try:
                result = llvm.validate_probe(0, '', obj, negative=False)
            except llvm.LLVMError as caught:
                error = str(caught)
            self.assertIsNone(error, 'actual x64 object must be verified after successful original owner cleanup')
            self.assertEqual(result['status'], 'compiled')
            for data in (b'', b'not-an-object', bytes.fromhex('4c010100') + bytes(32),
                         bytes.fromhex('64860100') + bytes(32)):
                obj.write_bytes(data)
                with self.subTest(data=data), self.assertRaises(llvm.LLVMError):
                    llvm.validate_probe(0, '', obj, negative=False)

    def test_positive_nonzero_bool_and_missing_object_are_never_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            obj = Path(temporary) / 'positive.obj'
            for status in (0, 1, 2, True, 0xC0000005):
                with self.subTest(status=status), self.assertRaises(llvm.LLVMError):
                    llvm.validate_probe(status, '', obj, negative=False)
            obj.write_bytes(coff_x64())
            for status in (1, True, 0xC0000005):
                with self.subTest(status=status), self.assertRaises(llvm.LLVMError):
                    llvm.validate_probe(status, '', obj, negative=False)


class LLVMToolAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.tools = {}
        for key, name in (('clang_cl', 'clang-cl.exe'), ('llvm_lib', 'llvm-lib.exe'),
                          ('llvm_ar', 'llvm-ar.exe'), ('lld_link', 'lld-link.exe'),
                          ('ninja', 'ninja.exe'), ('cmake', 'cmake.exe')):
            path = self.directory / 'LLVM' / 'bin' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pe_x64())
            self.tools[key] = str(path)

    def test_executable_receipt_reads_real_x64_file_and_records_only_provenance(self):
        path = Path(self.tools['clang_cl'])
        receipt = llvm.executable_receipt(path, name='clang-cl.exe', provenance='existing LLVM sibling')
        self.assertEqual(receipt, {'path': str(path.resolve()), 'bytes': 128,
                                  'sha256': hashlib.sha256(pe_x64()).hexdigest(),
                                  'machine': 'x86_64', 'provenance': 'existing LLVM sibling'})
        for contents in (b'not executable', pe_x64()[:66], pe_x64().replace(b'd\x86', b'L\x01')):
            path.write_bytes(contents)
            with self.subTest(contents=contents), self.assertRaises(llvm.LLVMError):
                llvm.executable_receipt(path, name='clang-cl.exe', provenance='existing')

    def test_executable_receipt_rejects_absent_directory_and_wrong_name(self):
        for path in (self.directory / 'absent.exe', self.directory, Path(self.tools['llvm_lib'])):
            with self.subTest(path=path), self.assertRaises(llvm.LLVMError):
                llvm.executable_receipt(path, name='clang-cl.exe', provenance='existing')

    def test_rejected_ninja_retains_bounded_header_receipt_without_running_any_tool(self):
        for machine, signature in ((0x014c, b'PE\0\0'), (0xaa64, b'PE\0\0'), (0x8664, b'PX\0\0')):
            with self.subTest(machine=machine, signature=signature):
                driver = self.prepare_driver()
                binary = bytearray(pe_x64())
                binary[64:68] = signature
                struct.pack_into('<H', binary, 68, machine)
                path = Path(self.tools['ninja'])
                path.write_bytes(binary)
                with self.assertRaisesRegex(llvm.LLVMError, 'ninja.exe: expected x64 PE executable'):
                    self.preflight(driver)
                report = json.loads((self.directory / 'modern-windows-llvm.json').read_text())
                self.assertFalse(report['successful'])
                self.assertEqual(self.calls, [], 'rejected executable must not start any owned command')
                self.assertNotIn('ninja', report['tools'])
                self.assertIn('ninja', report.get('rejected_tools', {}))
                self.assertEqual(report['rejected_tools']['ninja'], {
                    'path': str(path.resolve()), 'path_truncated': False,
                    'bytes': 128, 'sha256': hashlib.sha256(binary).hexdigest(),
                    'dos_signature_hex': '4d5a', 'pe_offset': 64,
                    'pe_signature_hex': signature.hex(), 'machine_code': machine})

    def test_rejected_receipt_caps_only_diagnostic_path_and_keeps_source_receipt_immutable(self):
        path = Path(self.tools['ninja'])
        binary = bytearray(pe_x64())
        struct.pack_into('<H', binary, 68, 0x014c)
        path.write_bytes(binary)
        canonical = {'path': 'C:\\' + 'x' * 2048, 'bytes': 128, 'sha256': 'a' * 64,
                     'provenance': 'existing child PATH'}
        before = canonical.copy()
        with patch.object(llvm, '_receipt', return_value=canonical), self.assertRaises(llvm.LLVMError) as caught:
            llvm.executable_receipt(path, name='ninja.exe', provenance='existing child PATH')
        diagnostic = getattr(caught.exception, 'rejected_receipt', None)
        self.assertIsNotNone(diagnostic, 'a hashed rejected executable needs a structured diagnostic receipt')
        self.assertEqual(diagnostic['path'], 'C:\\' + 'x' * 1021)
        self.assertTrue(diagnostic['path_truncated'])
        self.assertEqual(diagnostic['machine_code'], 0x014c)
        self.assertEqual(canonical, before)

    def test_archive_must_contain_the_exact_verified_positive_object(self):
        obj = self.directory / 'positive.obj'
        obj.write_bytes(coff_x64())
        archive = self.directory / 'positive.lib'
        archive.write_bytes(archive_member(coff_x64()))
        self.assertEqual(llvm.validate_archive(archive, obj)['status'], 'archived')
        for data in (b'', b'!<arch>\n', archive_member(b'other bytes'), archive_member(coff_x64())[:-1]):
            archive.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(llvm.LLVMError):
                llvm.validate_archive(archive, obj)

    def cache(self, **overrides):
        build = self.directory / 'build'
        build.mkdir(exist_ok=True)
        values = {'CMAKE_GENERATOR': 'Ninja', 'CMAKE_C_COMPILER': self.tools['clang_cl'],
                  'CMAKE_CXX_COMPILER': self.tools['clang_cl'], 'CMAKE_AR': self.tools['llvm_lib'],
                  'CMAKE_LINKER': self.tools['lld_link'], 'CMAKE_MAKE_PROGRAM': self.tools['ninja'],
                  'CMAKE_COMMAND': self.tools['cmake'], 'CMAKE_CACHE_MAJOR_VERSION': '4',
                  'CMAKE_CACHE_MINOR_VERSION': '4', 'CMAKE_CACHE_PATCH_VERSION': '5'}
        values.update(overrides)
        (build / 'CMakeCache.txt').write_text(''.join(key + ':FILEPATH=' + val + '\n' for key, val in values.items()))
        state = build / 'CMakeFiles' / '4.4.5'
        state.mkdir(parents=True, exist_ok=True)
        for language in ('C', 'CXX'):
            (state / ('CMake' + language + 'Compiler.cmake')).write_text(
                'set(CMAKE_' + language + '_COMPILER_ID "Clang")\n'
                'set(CMAKE_' + language + '_SIMULATE_ID "MSVC")\n'
                'set(CMAKE_' + language + '_COMPILER_FRONTEND_VARIANT "MSVC")\n'
                'set(CMAKE_' + language + '_COMPILER_LINKER "' + self.tools['lld_link'] + '")\n'
                'set(CMAKE_' + language + '_COMPILER_LINKER_ID "LLD")\n')
        return build

    def test_cmake_accepts_only_actual_clang_msvc_ninja_and_selected_llvm_paths(self):
        result = llvm.validate_cmake(self.cache(), self.tools)
        self.assertEqual(result['generator'], 'Ninja')
        self.assertEqual(result['languages']['C']['id'], 'Clang')
        self.assertEqual(result['languages']['CXX']['simulate_id'], 'MSVC')
        self.assertEqual(result['tools']['CMAKE_AR']['sha256'], hashlib.sha256(pe_x64()).hexdigest())

    def test_cmake_rejects_microsoft_compiler_archiver_linker_and_generator_fallback(self):
        for key, value in (('CMAKE_C_COMPILER', 'C:/VS/cl.exe'),
                           ('CMAKE_CXX_COMPILER', 'C:/VS/cl.exe'),
                           ('CMAKE_AR', 'C:/VS/lib.exe'), ('CMAKE_LINKER', 'C:/VS/link.exe'),
                           ('CMAKE_MAKE_PROGRAM', 'C:/VS/nmake.exe'),
                           ('CMAKE_GENERATOR', 'Visual Studio 18 2026')):
            with self.subTest(key=key), self.assertRaises(llvm.LLVMError):
                llvm.validate_cmake(self.cache(**{key: value}), self.tools)

    def test_cmake_rejects_missing_or_wrong_compiler_identity_even_with_good_paths(self):
        build = self.cache()
        identity = build / 'CMakeFiles/4.4.5/CMakeCCompiler.cmake'
        for value in ('', 'set(CMAKE_C_COMPILER_ID "MSVC")\n',
                      'set(CMAKE_C_COMPILER_ID "Clang")\nset(CMAKE_C_SIMULATE_ID "GNU")\n'):
            identity.write_text(value)
            with self.subTest(value=value), self.assertRaises(llvm.LLVMError):
                llvm.validate_cmake(build, self.tools)

    def test_cmake_rejects_actual_linker_override_despite_good_default_cache(self):
        for value in ('set(CMAKE_C_COMPILER_LINKER "C:/VS/link.exe")\n',
                      'set(CMAKE_C_COMPILER_LINKER_ID "MSVC")\n'):
            build = self.cache()
            identity = build / 'CMakeFiles/4.4.5/CMakeCCompiler.cmake'
            identity.write_text(identity.read_text() + value)
            with self.subTest(value=value), self.assertRaises(llvm.LLVMError):
                llvm.validate_cmake(build, self.tools)
        with self.assertRaises(llvm.LLVMError):
            llvm.validate_cmake(self.cache(CMAKE_LINKER_TYPE='MSVC'), self.tools)
        with self.assertRaises(llvm.LLVMError):
            llvm.validate_cmake(self.cache(CMAKE_C_COMPILER_AR='C:/VS/lib.exe'), self.tools)

    def test_cmake_must_be_the_actual_verified_executable_and_cache_version(self):
        for change in ({'CMAKE_COMMAND': 'C:/other/cmake.exe'}, {'CMAKE_CACHE_MAJOR_VERSION': '3'},
                       {'CMAKE_CACHE_MINOR_VERSION': '3'}, {'CMAKE_CACHE_PATCH_VERSION': '4'}):
            with self.subTest(change=change), self.assertRaises(llvm.LLVMError):
                llvm.validate_cmake(self.cache(**change), self.tools)

    def test_cmake_records_existing_sdk_helpers_and_accepts_verified_llvm_manifest_tool(self):
        sdk = self.directory / 'Windows Kits/10/bin/10.0/x64'
        sdk.mkdir(parents=True)
        for name in ('rc.exe', 'mt.exe'):
            (sdk / name).write_bytes(pe_x64())
        llvm_mt = Path(self.tools['clang_cl']).parent / 'llvm-mt.exe'
        llvm_mt.write_bytes(pe_x64())
        for manifest, version in ((sdk / 'mt.exe', '10.0.1.0'), (llvm_mt, '20.1.8.0')):
            with self.subTest(manifest=manifest), \
                    patch.object(llvm, '_windows_file_version', return_value=version):
                result = llvm.validate_cmake(self.cache(CMAKE_MT=str(manifest), CMAKE_RC_COMPILER=str(sdk / 'rc.exe')), self.tools)
                self.assertEqual(result['tools']['CMAKE_MT']['version'], version)
                self.assertEqual(result['tools']['CMAKE_RC_COMPILER']['path'], str(sdk / 'rc.exe'))
        with patch.object(llvm, '_windows_file_version', return_value='19.1.8.0'), self.assertRaises(llvm.LLVMError):
            llvm.validate_cmake(self.cache(CMAKE_MT=str(llvm_mt)), self.tools)

    def test_windows_version_resource_uses_fixed_dwords_and_rejects_invalid_or_oversize_queries(self):
        fixed = (ctypes.c_uint32 * 13)(0xFEEF04BD, 0, 0x00140001, 0x00080000)
        length = 52
        def query(buffer, key, output, count):
            self.assertEqual(key, '\\')
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(fixed)
            ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32)).contents.value = length
            return 1
        api = Mock()
        api.GetFileVersionInfoSizeW.return_value = 64
        api.GetFileVersionInfoW.return_value = 1
        api.VerQueryValueW.side_effect = query
        with patch.object(llvm.sys, 'platform', 'win32'), \
                patch.object(ctypes, 'WinDLL', return_value=api, create=True):
            self.assertEqual(llvm._windows_file_version('verified-helper.exe'), '20.1.8.0')
            for invalid_length in (0, 51, 65):
                length = invalid_length
                with self.subTest(length=length), self.assertRaises(llvm.LLVMError):
                    llvm._windows_file_version('verified-helper.exe')
            length = 52
            fixed[0] = 1
            with self.assertRaises(llvm.LLVMError):
                llvm._windows_file_version('verified-helper.exe')
            fixed[0] = 0xFEEF04BD
            for size in (0, 51, llvm.TEXT_LIMIT + 1):
                api.GetFileVersionInfoSizeW.return_value = size
                with self.subTest(size=size), self.assertRaises(llvm.LLVMError):
                    llvm._windows_file_version('verified-helper.exe')

    def test_conditional_llvm_rc_requires_sibling_version_and_frozen_selected_bytes(self):
        resource = Path(self.tools['clang_cl']).parent / 'llvm-rc.exe'
        resource.write_bytes(pe_x64())
        tools = dict(self.tools, llvm_rc=str(resource))
        selected = llvm.VerifiedTools(tools, {key: llvm.executable_receipt(value,
            name=Path(value).name, provenance='pure selected-tool fixture') for key, value in tools.items()})
        with patch.object(llvm, '_windows_file_version', return_value='20.1.8.0'):
            result = llvm.validate_cmake(self.cache(CMAKE_RC_COMPILER=str(resource)), selected)
            self.assertEqual(result['tools']['CMAKE_RC_COMPILER']['version'], '20.1.8.0')
            resource.write_bytes(pe_x64() + b'replaced same-version resource compiler')
            with self.assertRaises(llvm.LLVMError):
                llvm.validate_cmake(self.cache(CMAKE_RC_COMPILER=str(resource)), selected)
        with patch.object(llvm, '_windows_file_version', return_value='19.1.8.0'), self.assertRaises(llvm.LLVMError):
            llvm.validate_cmake(self.cache(CMAKE_RC_COMPILER=str(resource)), self.tools)

    def test_cache_audit_failure_is_retained_and_walk_errors_are_never_zero_caches(self):
        target = self.directory / 'target'
        target.mkdir()
        selected = llvm.VerifiedTools(self.tools, {key: llvm.executable_receipt(value,
            name=Path(value).name, provenance='pure fixture') for key, value in self.tools.items()})
        with patch.object(llvm.os, 'walk', side_effect=PermissionError('unreadable build subtree')):
            with self.assertRaises(PermissionError):
                llvm.audit_cargo(self.directory, target, selected, check_name='modern-rust-clippy')
        report = json.loads((self.directory / 'modern-windows-llvm-modern-rust-clippy-cmake.json').read_text())
        self.assertFalse(report['successful'])
        self.assertEqual(report['error_type'], 'PermissionError')

    def test_replacement_during_audit_never_leaves_successful_provenance_report(self):
        selected = llvm.VerifiedTools(self.tools, {key: llvm.executable_receipt(value,
            name=Path(value).name, provenance='pure fixture') for key, value in self.tools.items()})
        target = self.directory / 'target'
        target.mkdir()
        def walk(*args, **kwargs):
            Path(self.tools['clang_cl']).write_bytes(pe_x64() + b'changed during audit')
            return iter(())
        with patch.object(llvm.os, 'walk', side_effect=walk), self.assertRaises(llvm.LLVMError):
            llvm.audit_cargo(self.directory, target, selected, check_name='modern-rust-clippy')
        report = json.loads((self.directory / 'modern-windows-llvm-modern-rust-clippy-cmake.json').read_text())
        self.assertFalse(report['successful'])

    def test_report_write_failure_cannot_replace_original_owned_gate_failure(self):
        driver = self.prepare_driver(fail_label=llvm.NEGATIVE_NAME)
        with patch.object(llvm, '_write_json', side_effect=OSError('report inaccessible')), \
                redirect_stdout(io.StringIO()), self.assertRaisesRegex(release.ReleaseCheckError, 'original owned gate failure'):
            self.preflight(driver)

    def test_retention_copies_only_bounded_probe_artifacts_and_never_home_or_environment(self):
        source = self.directory / 'modern-windows-llvm'
        source.mkdir()
        (source / 'positive.c').write_text('int probe(void) { return 1; }\n')
        (source / 'negative.c').write_text('#error ' + llvm.NEGATIVE_MARKER + '\n')
        (source / 'positive.obj').write_bytes(coff_x64())
        (source / 'positive.lib').write_bytes(b'x' * (llvm.ARTIFACT_LIMIT + 1))
        (source / 'environment.txt').write_text('OTHER_SECRET=never-copy')
        (source / 'home').mkdir()
        (source / 'home/credentials').write_text('never-copy')
        log_name = 'modern-windows-llvm-negative.log'
        (self.directory / log_name).write_bytes(b'n' * (llvm.TEXT_LIMIT + 1))
        destination = self.directory / 'retained'
        destination.mkdir()
        llvm.retain_artifacts(self.directory, destination)
        kept = destination / 'modern-windows-llvm'
        self.assertEqual({p.name for p in kept.iterdir()}, {'positive.c', 'negative.c', 'positive.obj', 'retention.json'})
        retention = json.loads((kept / 'retention.json').read_text())
        self.assertEqual(retention['positive.lib']['status'], 'too_large')
        self.assertEqual(retention[log_name]['status'], 'truncated')
        self.assertEqual((destination / log_name).stat().st_size, llvm.TEXT_LIMIT)
        self.assertNotIn('never-copy', repr(list(kept.iterdir())) + (kept / 'retention.json').read_text())

    def prepare_driver(self, *, fail_label=None, clang_version='20.1.8'):
        root = self.directory / 'rust-1.99.1'
        rustc = root / 'bin/rustc.exe'
        rust_lld = root / 'lib/rustlib/x86_64-pc-windows-msvc/bin/rust-lld.exe'
        for path in (rustc, rust_lld, self.directory / 'rustup.exe', self.directory / 'nasm.exe'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pe_x64())
        self.tools.update(rustc=str(rustc), rust_lld=str(rust_lld), rustup=str(self.directory / 'rustup.exe'),
                          nasm=str(self.directory / 'nasm.exe'))
        self.calls = []
        def driver(command, *, name, environment, directory, timeout, shutdown_timeout, **private):
            self.calls.append((name, list(command), environment.copy(), timeout, shutdown_timeout, private))
            self.assertEqual(timeout, 17)
            self.assertEqual(shutdown_timeout, 15)
            if name == fail_label:
                raise release.ReleaseCheckError('original owned gate failure')
            if name.endswith('-rustc-path'): return str(rustc) + '\n'
            if name.endswith('-rustc-version'):
                return 'rustc 1.99.1 (pinned)\nhost: x86_64-pc-windows-msvc\nrelease: 1.99.1\nLLVM version: 23.1.1\n'
            if name.endswith('-rustc-sysroot'): return str(root) + '\n'
            if name.endswith('-clang-version'):
                return 'clang version ' + clang_version + '\nTarget: x86_64-pc-windows-msvc\n'
            if name.endswith('-archive-version'): return 'LLVM (https://llvm.org/):\n  LLVM version 20.1.8\n'
            if name.endswith('-linker-version'): return 'LLD 20.1.8\n'
            if name.endswith('-rust-linker-version'): return 'LLD 23.1.1 (compatible with link.exe)\n'
            if name.endswith('-cmake-version'): return 'cmake version 4.4.5\n'
            if name.endswith('-ninja-version'): return '1.13.2\n'
            if name.endswith('-nasm-version'): return 'NASM version 3.01 compiled on Oct 01 2026\n'
            if name.endswith('-nasm-positive'):
                Path(command[command.index('-o') + 1]).write_bytes(coff_x64())
                return ''
            if name.endswith('-positive'):
                output = next(value[3:] for value in command if value.startswith('/Fo'))
                Path(output).write_bytes(coff_x64())
                return ''
            if name == llvm.NEGATIVE_NAME:
                self.assertEqual(set(private), {'_llvm_negative_probe'})
                llvm.validate_negative_invocation(private['_llvm_negative_probe'], command, name=name,
                                                 directory=directory, platform='win32')
                return 'negative.c(1,1): error: ' + llvm.NEGATIVE_MARKER + '\n'
            if name.endswith('-archive'):
                output = next(value[5:] for value in command if value.startswith('/OUT:'))
                Path(output).write_bytes(archive_member(coff_x64()))
                return ''
            if name.endswith('-cmake-configure'):
                build = Path(command[command.index('-B') + 1])
                source = self.cache()
                import shutil
                shutil.copytree(source, build, dirs_exist_ok=True)
                return ''
            if name.endswith('-cmake-build'):
                build = Path(command[command.index('--build') + 1])
                (build / 'ori_llvm_probe.exe').write_bytes(pe_x64())
                (build / 'ori_llvm_probe.lib').write_bytes(archive_member(coff_x64()))
                return ''
            self.fail('unexpected native preflight command: ' + repr(command))
        self.which = {Path(value).name: value for value in self.tools.values()}
        return driver

    def preflight(self, driver):
        def owned(command, **kwargs):
            output = driver(command, **kwargs)
            code = 1 if kwargs['name'] == llvm.NEGATIVE_NAME else 0
            return output + f'[release-child-exit] decimal={code} hex=0x{code:08X}\n'
        with patch.object(llvm.shutil, 'which', side_effect=lambda name, path=None: self.which.get(name)), \
                patch.object(llvm.sys, 'platform', 'win32'):
            return llvm.preflight(self.directory, {'PATH': 'same-parent-path', 'OTHER_SECRET': 'never-report'},
                                  run_command=owned, timeout=17, shutdown_timeout=15)

    def test_preflight_uses_owned_commands_separate_probes_and_retains_literal_evidence(self):
        selected = self.preflight(self.prepare_driver())
        self.assertEqual(selected['clang_cl'], self.tools['clang_cl'])
        self.assertEqual(selected['rust_lld'], self.tools['rust_lld'])
        names = [entry[0] for entry in self.calls]
        self.assertLess(names.index('modern-windows-llvm-positive'), names.index(llvm.NEGATIVE_NAME))
        report_path = self.directory / 'modern-windows-llvm.json'
        report = json.loads(report_path.read_text())
        self.assertTrue(report['successful'])
        self.assertEqual(report['probes']['negative']['status'], 'expected_compiler_error')
        self.assertEqual(report['tools']['llvm_lib']['version_source'], 'identical llvm-ar executable bytes')
        self.assertIn('nasm', report['tools'])
        self.assertEqual(report['probes']['assembler']['status'], 'compiled')
        self.assertNotIn('OTHER_SECRET', report_path.read_text())
        self.assertNotIn('never-report', report_path.read_text())
        for entry in self.calls:
            if entry[0] != llvm.NEGATIVE_NAME:
                self.assertEqual(entry[-1], {})
        source = self.directory / 'modern-windows-llvm'
        self.assertIn('#include <windows.h>', (source / 'cmake/positive.c').read_text())
        self.assertEqual((source / 'negative.c').read_text(), '#error ' + llvm.NEGATIVE_MARKER + '\n')

    def test_missing_tool_wrong_version_and_mismatched_archive_binary_fail_closed(self):
        for condition in ('missing', 'version', 'archive'):
            with self.subTest(condition=condition):
                driver = self.prepare_driver(clang_version='19.1.8' if condition == 'version' else '20.1.8')
                if condition == 'missing': self.which.pop('ninja.exe')
                if condition == 'archive': Path(self.tools['llvm_lib']).write_bytes(pe_x64() + b'other')
                with self.assertRaises(llvm.LLVMError): self.preflight(driver)
                self.assertFalse(any(entry[0].endswith('-positive') for entry in self.calls))
                report = json.loads((self.directory / 'modern-windows-llvm.json').read_text())
                self.assertFalse(report['successful'])
                Path(self.tools['llvm_lib']).write_bytes(pe_x64())

    def test_preflight_never_reinterprets_an_original_owned_gate_failure(self):
        driver = self.prepare_driver(fail_label=llvm.NEGATIVE_NAME)
        with self.assertRaisesRegex(release.ReleaseCheckError, 'original owned gate failure'):
            self.preflight(driver)
        report = json.loads((self.directory / 'modern-windows-llvm.json').read_text())
        self.assertFalse(report['successful'])
        self.assertNotIn('negative', report['probes'])

    def test_zero_supervisor_footer_is_required_without_filtering_path_diagnostics(self):
        for extra in ('[release-child-exit] decimal=1 hex=0x00000001\n',
                      '[release-child-exit] decimal=0 hex=0x00000000\n', 'unexpected diagnostic\n'):
            with self.subTest(extra=extra):
                base = self.prepare_driver()
                def driver(command, **kwargs):
                    output = base(command, **kwargs)
                    return output + extra if kwargs['name'].endswith('-rustc-path') else output
                with self.assertRaises(llvm.LLVMError): self.preflight(driver)

    def test_absent_nasm_preserves_locked_prebuilt_path_and_requires_actual_cargo_copies(self):
        driver = self.prepare_driver()
        self.which.pop('nasm.exe')
        selected = self.preflight(driver)
        self.assertNotIn('nasm', selected)
        report = json.loads((self.directory / 'modern-windows-llvm.json').read_text())
        self.assertEqual(report['assembler']['status'], 'nasm_absent_prebuilt_pending')
        self.assertFalse(any('nasm-' in entry[0] for entry in self.calls))
        target = self.directory / 'target'
        build = target / 'debug/build/aws-lc-sys-fixture'
        output = build / 'out'
        output.mkdir(parents=True)
        (build / 'output').write_text("cargo:warning=Building with: CC\n"
            "cargo:warning=Target platform: 'x86_64-pc-windows-msvc'\n"
            "cargo:warning=NASM command not found or failed to execute.\n" + 'cargo:libdir=' + str(output) + '\n')
        copied = output / 'vpaes-x86_64.obj'
        copied.write_bytes(coff_x64())
        # Replace only the external pinned distribution's object manifest with
        # one literal COFF fixture. The production manifest has all 26 objects.
        fixtures = {'vpaes-x86_64.obj': hashlib.sha256(coff_x64()).hexdigest()}
        with patch.dict(llvm.PREBUILT_NASM, fixtures, clear=True):
            llvm.audit_cargo(self.directory, target, selected, check_name='modern-rust-clippy')
            audit = json.loads((self.directory / 'modern-windows-llvm-modern-rust-clippy-cmake.json').read_text())
            self.assertEqual(audit['assembler']['status'], 'locked_prebuilt_objects_verified')
            self.assertEqual(audit['assembler']['builds'][0]['objects'][0]['sha256'], fixtures['vpaes-x86_64.obj'])
            copied.write_bytes(coff_x64() + b'not the locked prebuilt object')
            with self.assertRaises(llvm.LLVMError):
                llvm.audit_cargo(self.directory, target, selected, check_name='modern-rust-tests')
            copied.write_bytes(coff_x64())
            (build / 'output').write_text('cargo:warning=NASM command not found or failed to execute.\n')
            with self.assertRaises(llvm.LLVMError):
                llvm.audit_cargo(self.directory, target, selected, check_name='modern-rust-tests')

    def test_preflight_rejects_tool_mutation_and_wrong_pinned_compiler_path(self):
        for condition in ('mutated_compiler', 'wrong_rustc_name'):
            with self.subTest(condition=condition):
                import shutil
                shutil.rmtree(self.directory / 'modern-windows-llvm', ignore_errors=True)
                Path(self.tools['clang_cl']).write_bytes(pe_x64())
                base = self.prepare_driver()
                def driver(command, **kwargs):
                    output = base(command, **kwargs)
                    if condition == 'wrong_rustc_name' and kwargs['name'].endswith('-rustc-path'):
                        return output.replace('rustc.exe', 'other.exe')
                    if condition == 'mutated_compiler' and kwargs['name'].endswith('-positive'):
                        Path(self.tools['clang_cl']).write_bytes(pe_x64() + b'changed after version probe')
                    return output
                with self.assertRaises(llvm.LLVMError): self.preflight(driver)
                report = json.loads((self.directory / 'modern-windows-llvm.json').read_text())
                self.assertFalse(report['successful'])

    def test_post_preflight_same_path_replacement_fails_before_cargo_and_during_cache_audit(self):
        from tools.modernization.checks import Check
        selected = self.preflight(self.prepare_driver())
        baseline = json.loads((self.directory / 'modern-windows-llvm.json').read_text())['tools']['clang_cl']['sha256']
        Path(self.tools['clang_cl']).write_bytes(pe_x64() + b'replaced after successful preflight')
        self.assertNotEqual(hashlib.sha256(Path(self.tools['clang_cl']).read_bytes()).hexdigest(), baseline)
        target = self.directory / 'target'
        target.mkdir()
        for action in (lambda: llvm.validate_cmake(self.cache(), selected),
                       lambda: llvm.audit_cargo(self.directory, target, selected, check_name='modern-rust-clippy')):
            with self.subTest(action=action), self.assertRaises(llvm.LLVMError): action()
        commands_started = []
        def run(command, **kwargs):
            commands_started.append(kwargs['name'])
            return '# tests 1\n# pass 1\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n'
        plan = [Check('modern-rust-clippy', ['/prepared/cargo.exe', 'clippy'], 'node')]
        with patch.object(release.sys, 'platform', 'win32'), \
                patch.object(release, 'modern_commands', return_value=plan), \
                patch.object(llvm, 'preflight', return_value=selected), \
                patch.object(release, 'run_command', side_effect=run):
            with self.assertRaises(release.ReleaseCheckError):
                release.run_modern_checks(self.directory, {'CARGO_TARGET_DIR': str(target)},
                    research_python='prepared', swift=False, command_timeout=17, shutdown_timeout=15, windows_llvm=True)
        self.assertEqual(commands_started, [])

    def test_modern_runner_selects_only_cargo_children_and_preserves_parent(self):
        from tools.modernization.checks import Check
        rust_lld = self.directory / 'rust-lld.exe'
        rust_lld.write_bytes(pe_x64())
        self.tools['rust_lld'] = str(rust_lld)
        selected = llvm.VerifiedTools(self.tools, {key: llvm.executable_receipt(value,
            name=Path(value).name, provenance='pure fixture') for key, value in self.tools.items()})
        parent = {'PATH': 'same', 'INCLUDE': 'same-sdk', 'LIB': 'same-library',
                  'CARGO_TARGET_DIR': str(self.directory / 'target')}
        Path(parent['CARGO_TARGET_DIR']).mkdir()
        original = parent.copy()
        names = ['modern-ui-inputs', 'modern-cargo', 'modern-rust-format', 'modern-rust-clippy',
                 'modern-rust-tests', 'modern-scientific-http', 'modern-core-client-interop']
        checks = [Check(name, ['prepared-' + name], 'node') for name in names]
        captured = {}
        def run(command, *, name, environment, **kwargs):
            captured[name] = environment.copy()
            return '# tests 1\n# pass 1\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n'
        with patch.object(release.sys, 'platform', 'win32'), \
                patch.object(release, 'modern_commands', return_value=checks), \
                patch.object(llvm, 'preflight', return_value=selected), \
                patch.object(release, 'run_command', side_effect=run):
            release.run_modern_checks(self.directory, parent, research_python='prepared', swift=False,
                command_timeout=17, shutdown_timeout=15, windows_llvm=True)
        self.assertEqual(parent, original)
        for name in names:
            with self.subTest(name=name):
                if name in ('modern-rust-clippy', 'modern-rust-tests'):
                    self.assertEqual(captured[name]['CC_x86_64_pc_windows_msvc'], self.tools['clang_cl'])
                    self.assertEqual(captured[name]['CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER'], str(rust_lld))
                else: self.assertEqual(captured[name], original)


class LLVMOwnedNegativeProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.compiler = self.directory / 'clang-cl.exe'
        self.compiler.write_bytes(pe_x64())
        self.probe = llvm.prepare_negative_probe(self.directory, {'clang_cl': str(self.compiler)})

    def run_probe(self, status=1, *, active=False, wait_error=None, assign_error=None, cleanup_error=None,
                  marker=True, object_output=False, name='modern-windows-llvm-negative'):
        job = Mock()
        job.active.return_value = 0
        job.assign.side_effect = assign_error
        process = Mock()
        process.returncode = status
        process.stdin.closed = True
        process.wait.side_effect = [wait_error, None] if wait_error else None
        events = []
        def spawn(*args, **kwargs):
            events.append('spawn')
            kwargs['stdout'].write(('negative.c(1,1): error: ' + llvm.NEGATIVE_MARKER + '\n') if marker else 'unrelated error\n')
            if object_output:
                self.probe.obj.write_bytes(coff_x64())
            return process
        with patch.object(release.sys, 'platform', 'win32'), \
                patch.object(release, 'WindowsJob', return_value=job), \
                patch.object(release, '_process_options', return_value={}), \
                patch.object(release, '_job_still_active', return_value=active), \
                patch.object(release, '_stop_command', side_effect=cleanup_error), \
                patch.object(release.subprocess, 'Popen', side_effect=spawn), redirect_stdout(io.StringIO()):
            output = release.run_command(self.probe.command, name=name, environment={}, directory=self.directory,
                                         timeout=7, _llvm_negative_probe=self.probe)
        self.assertIn(llvm.NEGATIVE_MARKER, output)
        job.close.assert_called_once_with()
        return events

    def test_exact_expected_compiler_error_passes_only_after_original_job_closure(self):
        self.assertEqual(self.run_probe(), ['spawn'])

    def test_original_timeout_assignment_and_descendant_gates_are_not_expected_errors(self):
        cases = ({'wait_error': subprocess.TimeoutExpired('owned', 7)},
                 {'assign_error': OSError('assignment failed')}, {'active': True},
                 {'active': True, 'cleanup_error': OSError('cleanup failed')})
        for case in cases:
            with self.subTest(case=case), self.assertRaises((release.ReleaseCheckError, subprocess.TimeoutExpired, OSError)):
                self.run_probe(**case)

    def test_zero_crash_other_error_and_emitted_object_cannot_pass(self):
        for case in ({'status': 0}, {'status': 2}, {'status': 0xC0000005}, {'status': True},
                     {'marker': False}, {'object_output': True}):
            with self.subTest(case=case), self.assertRaises((release.ReleaseCheckError, llvm.LLVMError)):
                self.run_probe(**case)
            self.probe.obj.unlink(missing_ok=True)

    def test_wrong_label_platform_source_or_compiler_are_rejected_before_spawn(self):
        for mutation in ('label', 'platform', 'source', 'compiler', 'stale_object', 'token'):
            with self.subTest(mutation=mutation):
                self.compiler.write_bytes(pe_x64())
                self.probe = llvm.prepare_negative_probe(self.directory, {'clang_cl': str(self.compiler)})
                if mutation == 'source': self.probe.source.write_text('int not_a_negative_probe;\n')
                if mutation == 'compiler': self.compiler.write_bytes(b'changed')
                if mutation == 'stale_object': self.probe.obj.write_bytes(coff_x64())
                with patch.object(release.sys, 'platform', 'linux' if mutation == 'platform' else 'win32'), \
                        patch.object(release.subprocess, 'Popen') as spawn, \
                        self.assertRaises((release.ReleaseCheckError, llvm.LLVMError)):
                    release.run_command(self.probe.command, name='other' if mutation == 'label' else llvm.NEGATIVE_NAME,
                        environment={}, directory=self.directory, timeout=7,
                        _llvm_negative_probe=object() if mutation == 'token' else self.probe)
                spawn.assert_not_called()
                self.probe.obj.unlink(missing_ok=True)

    def test_original_callers_still_reject_exit_one_with_the_same_marker(self):
        job = Mock()
        job.active.return_value = 0
        process = Mock(returncode=1)
        process.returncode = 1
        with patch.object(release.sys, 'platform', 'win32'), \
                patch.object(release, 'WindowsJob', return_value=job), \
                patch.object(release, '_process_options', return_value={}), \
                patch.object(release.subprocess, 'Popen', return_value=process), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(release.ReleaseCheckError, 'exited with status 1'):
                release.run_command(self.probe.command, name=llvm.NEGATIVE_NAME, environment={},
                                    directory=self.directory, timeout=7)

    def test_literal_negative_source_keeps_lf_bytes_when_text_writes_translate_like_windows(self):
        write_text = Path.write_text
        def windows_text(path, data, *args, **kwargs):
            return write_text(path, data.replace('\n', '\r\n'), *args, **kwargs)
        with patch.object(Path, 'write_text', new=windows_text):
            probe = llvm.prepare_negative_probe(self.directory, {'clang_cl': str(self.compiler)})
        self.assertEqual(probe.source.read_bytes(), b'#error ORI_WINDOWS_LLVM_EXPECTED_PREPROCESSOR_ERROR\n')
        llvm.validate_negative_invocation(probe, probe.command, name=llvm.NEGATIVE_NAME,
                                         directory=self.directory, platform='win32')


if __name__ == '__main__':
    unittest.main()
