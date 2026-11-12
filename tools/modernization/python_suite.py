"""Discover an isolated Python subsuite and persist its real outcome, including failures."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import sys
import unittest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    selected=parser.add_mutually_exclusive_group(required=True)
    selected.add_argument('--start-directory',type=Path)
    selected.add_argument('--module')
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--research',action='store_true')
    parser.add_argument('--core-prefix')
    args=parser.parse_args(argv)
    report={'python':sys.version,'prefix':sys.prefix,'successful':False,'discovered_tests':0,'tests_run':0,'skipped':[]}
    try:
        if args.research:
            if tuple(sys.version_info[:3]) != (3,15,1):
                raise RuntimeError('Research checks require Python 3.15.1')
            if sys.prefix == sys.base_prefix or not args.core_prefix or Path(sys.prefix).resolve() == Path(args.core_prefix).resolve():
                raise RuntimeError('Research interpreter must be an isolated venv, separate from the core')
            expected={'numpy':'2.5.4','duckdb':'2.0.0','pyarrow':'26.0.1'}
            report['packages']={key:importlib.metadata.version(key) for key in expected}
            if report['packages'] != expected:
                raise RuntimeError('Research dependency versions do not match the hashed lock')
        loader=unittest.TestLoader()
        suite=loader.discover(str(args.start_directory),pattern='test_*.py') if args.start_directory else loader.loadTestsFromName(args.module)
        report['discovered_tests']=suite.countTestCases()
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        report.update(tests_run=result.testsRun,skipped=[(case.id(),reason) for case,reason in result.skipped],
                      failures=len(result.failures),errors=len(result.errors))
        report['successful']=result.wasSuccessful() and result.testsRun>0 and result.testsRun==report['discovered_tests'] and not result.skipped
    except Exception as error:
        report['error']=f'{type(error).__name__}: {error}'
        print(report['error'],file=sys.stderr,flush=True)
    finally:
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0 if report['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
