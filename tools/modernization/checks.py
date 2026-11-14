"""Modern command descriptions and result validation; execution stays in the release runner."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import sys

from tools.modernization.swift_version import is_pinned_swift_output


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]
    kind: str = 'command'
    report: Path | None = None
    version: str | None = None
    environment: dict[str, str] = field(default_factory=dict)


def tool(name, environment):
    path = shutil.which(name, path=environment.get('PATH'))
    if path is None:
        raise ValueError(f'Required modernization tool {name!r} is missing; prepare dependencies before running checks')
    return path


def npm_command(node, environment):
    executable = Path(tool('npm', environment))
    # Invoke npm's JS entry directly: .cmd wrappers need a shell on Windows.
    candidates = [executable.resolve(), executable.parent/'node_modules/npm/bin/npm-cli.js']
    for candidate in candidates:
        if candidate.name == 'npm-cli.js' and candidate.is_file():
            return [node, str(candidate)]
    raise ValueError('Cannot find npm-cli.js beside npm; install Node/npm before running checks')


def commands(root, directory, environment, *, research_python, swift):
    node, cargo, rustc = (tool(name,environment) for name in ('node','cargo','rustc'))
    research = tool(research_python,environment)
    npm = npm_command(node,environment)
    helper = str(root/'tools/modernization/python_suite.py')
    def suite(name, *, start=None, module=None, interpreter=sys.executable, extra=(), env=None):
        report=directory/(name+'.json')
        command=[interpreter,helper,'--report',str(report)]
        command += ['--start-directory',str(root/start)] if start else ['--module',module]
        return Check(name,command+list(extra),'python',report,environment=env or {})
    result = [
        Check('modern-ui-inputs',[sys.executable,str(root/'tools/build_modern_web.py'),'--check']),
        Check('modern-node',[node,'--version'],'version',version=r'^v26\.13\.0\s*$'),
        Check('modern-rustc',[rustc,'--version'],'version',version=r'^rustc 1\.99\.1(?:\s|$)'),
        Check('modern-cargo',[cargo,'--version'],'version',version=r'^cargo 1\.99\.1(?:\s|$)'),
        Check('modern-typescript-installed',[node,str(root/'tools/modernization/node_versions.cjs'),str(root)]),
        Check('modern-generated-contracts',[sys.executable,str(root/'tools/contracts/generate.py'),'--check']),
        Check('modern-protobuf-fixtures',[sys.executable,str(root/'contracts/protobuf/generate_stream_fixtures.py'),'--check']),
        suite('modern-openapi',module='tests.test_openapi_contract'),
        suite('modern-python-contracts',start='contracts/tests'),
        Check('modern-rust-format',[cargo,'fmt','--all','--','--check']),
        Check('modern-rust-clippy',[cargo,'clippy','--workspace','--all-features','--all-targets','--locked','--offline','-j','2','--','-D','warnings']),
        Check('modern-rust-tests',[cargo,'test','--workspace','--all-features','--locked','--offline','-j','2'],'rust',
              environment={'ORI_RESEARCH_PYTHON':research,'ORI_TASKS_TEST_PYTHON':sys.executable,
                           'ORI_TEST_PYTHON':sys.executable}),
        suite('modern-scientific-http',start='crates/ori-core/tests',
              env={'ORI_CORE_TEST_BINARY':str(Path(environment.get('CARGO_TARGET_DIR',root/'target'))/'debug'/('ori-core.exe' if os.name=='nt' else 'ori-core')),
                   'ORI_TEST_ANALYSIS_PYTHON':research}),
        Check('modern-typescript-compile',npm+['--prefix',str(root/'contracts/generated/typescript'),'run','check']),
        Check('modern-typescript-tests',[node,'--test','--test-reporter=tap',str(root/'contracts/tests/typescript.test.ts')],'node'),
        Check('modern-protobuf-typescript-tests',[node,'--test','--test-reporter=tap',str(root/'crates/ori-stream/tests/typescript/wire.test.ts')],'node'),
        suite('modern-research',start='research/tests',interpreter=research,
              extra=['--research','--core-prefix',sys.prefix],
              env={'PYTHONPATH':str(root/'research/src')}),
        suite('modern-scientific-rpc',module='tests.test_research_rpc',interpreter=research,
              extra=['--research','--core-prefix',sys.prefix]),
    ]
    report=directory/'modern-web-tests.json'
    result += [Check('modern-web-tests',npm+['--prefix',str(root/'clients/web'),'test','--','--reporter=json','--outputFile',str(report)],'vitest',report),
               Check('modern-web-build',npm+['--prefix',str(root/'clients/web'),'run','build','--','--outDir',str(directory/'modern-web-build'),'--emptyOutDir'])]
    if swift:
        executable=tool('swift',environment)
        result += [Check('modern-swift-version',[executable,'--version'],'swift-version'),
                   Check('modern-swift-contracts',[executable,'test','--package-path',str(root/'contracts/tests/swift-goldens'),
                         '--scratch-path',str(directory/'modern-swift-build'),'-j','2'],'swift'),
                   Check('modern-swift-protobuf',[executable,'test','--package-path',str(root/'contracts/protobuf/swift-stream'),
                         '--scratch-path',str(directory/'modern-swift-protobuf-build'),'-j','2'],'swift')]
    report=directory/'modern-core-client-interop.json'
    candidate=Path(environment.get('CARGO_TARGET_DIR',root/'target'))/'debug'/('ori-core.exe' if os.name=='nt' else 'ori-core')
    command=[sys.executable,str(root/'tools/modernization/client_interop.py'),
             '--candidate',str(candidate),'--node',node,'--count','1205','--renderer','canvas',
             '--artifacts',str(directory/'modern-core-client-interop'),'--report',str(report)]
    if swift:command += ['--swift-builder',executable]
    result.append(Check('modern-core-client-interop',command,
                        'client-interop-swift' if swift else 'client-interop-web',report))
    return result


def count_tests(kind, output, report=None):
    if kind == 'swift-version':
        if not is_pinned_swift_output(output):
            raise ValueError('Swift compiler output does not match the pinned 6.4.2 toolchain')
        return None
    if kind in ('client-interop-web','client-interop-swift'):
        if report is None:raise ValueError('Missing real-Core client interoperability report')
        from tools.modernization.client_interop import verify_summary
        raw=report.read_bytes()
        if len(raw)>65536:raise ValueError('Client interoperability summary exceeded its byte bound')
        verify_summary(json.loads(raw),swift=kind=='client-interop-swift')
        return None  # Functional proof, not an invented framework-test count.
    if kind == 'rust':
        summaries=re.findall(r'test result: ok\. (\d+) passed; (\d+) failed; (\d+) ignored; (\d+) measured; (\d+) filtered out',output)
        if not summaries or any(any(int(value) for value in row[1:]) for row in summaries):
            raise ValueError('Rust tests missing, failed, ignored, measured or filtered')
        count=sum(int(row[0]) for row in summaries)
    elif kind == 'node':
        values={name:int(value) for name,value in re.findall(r'^# (tests|pass|fail|cancelled|skipped|todo) (\d+)\s*$',output,re.M)}
        if set(values) != {'tests','pass','fail','cancelled','skipped','todo'} or values['tests'] != values['pass'] or any(values[k] for k in ('fail','cancelled','skipped','todo')):
            raise ValueError('Node tests missing, failed, cancelled, skipped or pending')
        count=values['tests']
    elif kind == 'swift':
        if any(re.search(r'\bskipped\b',line,re.I) and not re.search(r'\b(?:with )?0 tests? skipped\b',line) for line in output.splitlines()):
            raise ValueError('Swift tests skipped')
        counts=re.findall(r'Executed (\d+) tests?',output)
        count=max(map(int,counts),default=0)
    elif kind in ('python','vitest'):
        if report is None:
            raise ValueError('Missing test report path')
        value=json.loads(report.read_text(encoding='utf-8'))
        if not isinstance(value,dict):
            raise ValueError('Expected a structured test result object')
        if kind == 'python':
            count=value.get('tests_run')
            if value.get('successful') is not True or type(value.get('discovered_tests')) is not int or count != value.get('discovered_tests') or value.get('skipped') != [] or any(type(value.get(key)) is not int or value[key] != 0 for key in ('failures','errors')):
                raise ValueError('Python discovery/execution report failed or contains skips')
        else:
            count=value.get('numTotalTests')
            if value.get('success') is not True or type(value.get('numPassedTests')) is not int or count != value.get('numPassedTests') or any(type(value.get(key)) is not int or value[key] != 0 for key in ('numFailedTests','numPendingTests','numTodoTests')):
                raise ValueError('Web test report failed or contains pending tests')
    else:
        return None
    if type(count) is not int or count <= 0:
        raise ValueError('No executed tests reported')
    return count
