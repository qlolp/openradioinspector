"""Bind C/C++ to the selected Xcode SDK before Cargo or native builds."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def prepare(environment, *, run=subprocess.run):
    environment = dict(environment)
    developer = Path(environment['DEVELOPER_DIR']).resolve(strict=True)
    if not developer.is_dir():
        raise ValueError('The selected Xcode developer directory is unavailable')
    query_environment = environment | {'DEVELOPER_DIR': str(developer)}
    query_environment.pop('SDKROOT', None)

    def query(*arguments):
        result = run(['xcrun', *arguments], env=query_environment, check=True,
                     capture_output=True, text=True, timeout=30)
        value = result.stdout.strip()
        if not value or len(value) > 4096 or any(char in value for char in '\r\n\0'):
            raise ValueError('Invalid selected Xcode SDK/tool path')
        path = Path(value)
        if not path.is_absolute() or not path.resolve(strict=True).is_relative_to(developer):
            raise ValueError('SDK and compilers must belong to the selected Xcode')
        return path

    sdk = query('--sdk', 'macosx', '--show-sdk-path').resolve(strict=True)
    header = sdk / 'usr/include/stdio.h'
    if not header.is_file() or not header.resolve().is_relative_to(sdk):
        raise ValueError('The selected macOS SDK stdio.h is unavailable')
    compilers = {}
    for key, name in (('CC', 'clang'), ('CXX', 'clang++')):
        compiler = query('--toolchain', 'XcodeDefault', '--sdk', 'macosx', '--find', name)
        # cc-rs adds -isysroot on macOS for an explicit .xctoolchain tool path.
        if 'XcodeDefault.xctoolchain' not in compiler.parts or not compiler.is_file() \
                or not os.access(compiler, os.X_OK):
            raise ValueError('C/C++ compilers must use the selected Xcode default toolchain')
        compilers[key] = str(compiler)
    exports = {'SDKROOT': str(sdk), **compilers}
    with tempfile.TemporaryDirectory(prefix='native-sdk-', dir=environment['RUNNER_TEMP']) as temporary:
        work = Path(temporary)
        for name in ('home', 'tmp'):
            (work / name).mkdir(mode=0o700)
        source, output = work / 'headers.c', work / 'headers.o'
        source.write_text('#include <stdio.h>\n#include <stdint.h>\n#include <stdlib.h>\n'
                          'int sdk_headers(void) { return sizeof(uint32_t) == 4 ? EXIT_SUCCESS : EXIT_FAILURE; }\n',
                          encoding='utf-8')
        compile_environment = environment | exports | {'HOME': str(work / 'home'),
            'TMPDIR': str(work / 'tmp'), 'TEMP': str(work / 'tmp'), 'TMP': str(work / 'tmp')}
        run([exports['CC'], '-isysroot', exports['SDKROOT'], '-Werror', '-x', 'c', '-c', str(source), '-o', str(output)],
            cwd=work, env=compile_environment, check=True, capture_output=True, text=True, timeout=30)
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError('The selected Xcode C-header preflight produced no object')
    # Publish only after lookup, containment, header and real compile all pass.
    with Path(environment['GITHUB_ENV']).open('a', encoding='utf-8') as stream:
        for key, value in exports.items():
            stream.write(f'{key}={value}\n')
    return exports | {'stdio_header': str(header.resolve()),
                      'CC_canonical': str(Path(exports['CC']).resolve()),
                      'CXX_canonical': str(Path(exports['CXX']).resolve()),
                      'C_header_preflight': 'passed'}


if __name__ == '__main__':
    try:
        print(json.dumps(prepare(os.environ), sort_keys=True))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'Selected Xcode C SDK preflight failed: {error}', file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr[:16384], file=sys.stderr)
        raise SystemExit(1)
