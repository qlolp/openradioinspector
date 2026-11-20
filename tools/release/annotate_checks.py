"""Expose owned synthetic failures in GitHub checks when artifact logs are unavailable."""
from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import re

FAILURE = re.compile(r'(^FAIL:|^ERROR:|^FAILED\b|AssertionError:|Expectation failed|panicked at|\berror:|timed out|deadline exceeded)', re.I)


def escape(value):
    return str(value).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def report(root):
    root = Path(root)
    summaries = []
    for path in sorted(root.rglob('summary.json'))[:32]:
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if value.get('successful') is False:
            summaries.append((path.parent, value))
    count = 0
    seen = set()

    def emit(message):
        nonlocal count
        message = str(message)[:3500]
        if count >= 20 or message in seen:
            return
        seen.add(message); count += 1
        print('::error::' + escape(message), flush=True)

    for directory, value in summaries:
        emit(value.get('error') or 'Synthetic release checks failed')
        for error in value.get('cleanup_errors', []):
            emit('Cleanup: ' + str(error))
        for path in sorted(directory.glob('*.log')):
            # Reports are private to this CI run; a bounded tail includes the
            # unittest summary without retaining an entire verbose suite in RAM.
            with path.open(encoding='utf-8', errors='replace') as stream:
                lines = list(deque(stream, maxlen=3000))
            index = 0
            while index < len(lines):
                if FAILURE.search(lines[index]):
                    end = min(len(lines), index + 12)
                    emit(path.name + ':\n' + ''.join(lines[index:end]))
                    index = end
                else:
                    index += 1
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    report(args.directory)
