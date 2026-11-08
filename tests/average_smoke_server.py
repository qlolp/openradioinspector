"""Synthetic-only fixture for client smoke tests; never opens a user archive.

Run: python -m tests.average_smoke_server --port 56037
Stop with Ctrl-C. The complete temporary SQLite dump must remain unchanged.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from radioinspector.session_service import SessionService
from tests.smoke_fixture_support import run_fixture


def _populate(service: SessionService) -> dict:
    base = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    # Independent bounded-power oracle: the production core uses log-sum-exp.
    offset = 10 * math.log10((0.1 + 1 + 10) / 3)
    for suffix in ('one', 'two'):
        session = service.create_session({
            'id': f'average-{suffix}', 'name': f'Synthetic average {suffix}',
            'streams': [
                {'id': f'spectrum-{suffix}', 'device': 'Synthetic tinySA',
                 'role': 'spectrum_analyzer', 'level_unit': 'dBm',
                 'capabilities': ['sweep', 'waterfall']},
                {'id': f'receiver-{suffix}', 'device': 'Synthetic AR-DV1',
                 'role': 'receiver', 'level_unit': 's_meter_count'},
            ],
        })
        service.start_session(session.id, at=base)
        for number in range(1, 5):
            frequencies = [100_000_000 + 100_000 * i for i in range(101)]
            levels = [-100 + i / 10 + ([-10, 0, 10][number - 1] if number < 4 else offset)
                      for i in range(101)]
            if number == 4:
                levels[30] += 20
                levels[70] -= 10
            service.ingest_sweep({
                'id': f'{suffix}-sweep-{number}', 'session_id': session.id,
                'stream_id': f'spectrum-{suffix}',
                'interval': {
                    'started_at': (base + timedelta(seconds=number * 10)).isoformat(),
                    'ended_at': (base + timedelta(seconds=number * 10 + 1)).isoformat(),
                },
                'frequencies_hz': frequencies, 'levels': levels, 'level_unit': 'dBm',
                'settings': {'adapter': 'synthetic-fixture', 'rbw_hz': 10_000,
                             'sequence': number, 'simulated': True, 'generator_output': 'off'},
            })
        service.ingest_observation({
            'id': f'{suffix}-observation', 'session_id': session.id,
            'stream_id': f'receiver-{suffix}',
            'interval': {'started_at': base.isoformat(),
                         'ended_at': (base + timedelta(seconds=1)).isoformat()},
            'frequency_hz': 145_500_000, 'mode': 'FM', 'level': 37,
            'level_unit': 's_meter_count', 'squelch_open': True,
        })
    service.create_sweep_reference('average-one', {
        'name': 'Synthetic retained reference', 'sweep_id': 'one-sweep-1',
    })
    return {
        'session': 'average-one', 'stream': 'spectrum-one',
        'reference_ids': ['one-sweep-1', 'one-sweep-2', 'one-sweep-3'],
        'target_id': 'one-sweep-4', 'points': 101,
        'mean_at_first_bin_dbm': -100 + offset,
        'expected_maximum_db': 20, 'maximum_frequency_hz': 103_000_000,
        'expected_minimum_db': -10, 'minimum_frequency_hz': 107_000_000,
    }


def main() -> None:
    run_fixture(_populate, default_port=56037, prefix='openradio-average-smoke-', description=__doc__)


if __name__ == '__main__':
    main()
