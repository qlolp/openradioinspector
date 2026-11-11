"""Reproducible core ingest/history baseline; never opens a physical device.

Run: python -m tests.modernization_baseline --report-dir <new-directory>
Numbers describe this host and workload, not Pi/UI/hardware acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import time

import numpy as np

from radioinspector.contracts import Sweep, TimeInterval
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.monitoring_endurance import _process_peak_rss

START = datetime(2026, 11, 11, tzinfo=timezone.utc)


def source(index, bins):
    started = START + timedelta(milliseconds=100 * index)
    return Sweep(id=f'baseline-{index:08d}', session_id='baseline-session',
        stream_id='baseline-analyzer', interval=TimeInterval(started, started+timedelta(milliseconds=70)),
        frequencies_hz=tuple(100_000_000 + 10_000 * column for column in range(bins)),
        levels=tuple(-100.0 + ((index+column) % 32) / 8 for column in range(bins)),
        level_unit='dBm', settings={'adapter': 'synthetic-modernization', 'simulated': True,
                                   'generator_output': False})


def run(directory: Path, *, sweeps=128, bins=290):
    if type(sweeps) is not int or not 1 <= sweeps <= 100_000:
        raise ValueError('sweeps must be an integer in 1..100000')
    if type(bins) is not int or not 1 <= bins <= 65_536:
        raise ValueError('bins must be an integer in 1..65536')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / 'archive.sqlite'
    descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    service = SessionService(store=SQLiteSessionStore(str(archive)), history_limit=64,
                             clock=lambda: START)
    durations = []
    history_ms = 0.0
    verified = 0
    started = time.perf_counter()
    try:
        service.create_session({'id': 'baseline-session', 'name': 'Synthetic baseline',
            'streams': [{'id': 'baseline-analyzer',
                'device': 'synthetic', 'role': 'spectrum_analyzer', 'level_unit': 'dBm'}]})
        for index in range(sweeps):
            value = source(index, bins)
            before = time.perf_counter()
            service.ingest_sweep(value)
            durations.append((time.perf_counter()-before)*1000)
        before = time.perf_counter()
        service.list_sweeps('baseline-session', limit=100)
        history_ms = (time.perf_counter()-before)*1000
        persisted = service._store._connection.execute('SELECT COUNT(*) FROM sweeps').fetchone()[0]
        for index in range(sweeps):
            value = service._store.get_sweep(f'baseline-{index:08d}', session_id='baseline-session')
            if value != source(index, bins):
                raise AssertionError(f'Raw source changed: {index}')
            verified += 1
        if persisted != sweeps or verified != sweeps:
            raise AssertionError(f'Offered {sweeps}, persisted {persisted}, verified {verified}')
    finally:
        service.close()
    ordered = sorted(durations)
    return {'report_version': 1, 'hardware_tested': False, 'ui_tested': False,
        'runtime': {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version,
                    'numpy': np.__version__, 'platform': platform.platform()},
        'workload': {'sweeps': sweeps, 'bins': bins, 'synthetic': True},
        'sources': {'offered': sweeps, 'persisted': persisted, 'verified': verified},
        'raw_preserved': True, 'archive_bytes': archive.stat().st_size,
        'elapsed_seconds': time.perf_counter()-started, 'peak_rss_bytes': _process_peak_rss(),
        'commit_latency_ms': {'median': statistics.median(ordered),
                              'p95': ordered[math.ceil(len(ordered)*0.95)-1]},
        'history_first_100_ms': history_ms}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--sweeps', type=int, default=128)
    parser.add_argument('--bins', type=int, default=290)
    args = parser.parse_args(argv)
    report = run(args.report_dir, sweeps=args.sweeps, bins=args.bins)
    output = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    (args.report_dir/'baseline.json').write_text(output, encoding='utf-8')
    print(output, end='')


if __name__ == '__main__':
    main()
