"""Run a native build unchanged, also exposing Swift errors as check annotations."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
ERROR = re.compile(r'(?P<file>[^\r\n]+\.swift):(?P<line>\d+)(?::(?P<column>\d+))?: error: (?P<message>.*)')


def escape(value, *, property=False):
    value = str(value).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    return value.replace(':', '%3A').replace(',', '%2C') if property else value


def annotation(line):
    match = ERROR.search(line)
    if not match:
        return None
    path = Path(match['file'])
    if path.is_absolute():
        try:
            path = path.relative_to(ROOT)
        except ValueError:
            return '::error::' + escape(match['message'][:4000])
    column = f',col={match["column"]}' if match['column'] else ''
    return (f'::error file={escape(path.as_posix(), property=True)},line={match["line"]}{column}::'
            + escape(match['message'][:4000]))


def report_log_annotations(directory):
    errors = set()
    for path in sorted(Path(directory).glob('*.log')):
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                error = annotation(line)
                if error and error not in errors and len(errors) < 30:
                    errors.add(error)
                    print(error, flush=True)
    return len(errors)


def main(argv=None):
    command = list(sys.argv[1:] if argv is None else argv)
    if command[:1] == ['--']:
        command.pop(0)
    if not command:
        raise SystemExit('A command is required after --')
    emitted = set()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace') as process:
        for line in process.stdout:
            print(line, end='', flush=True)
            error = annotation(line)
            if error and error not in emitted and len(emitted) < 30:
                emitted.add(error)
                print(error, flush=True)
        return process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
