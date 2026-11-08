"""Synthetic-only sweep-context HTTP fixture; never opens a user archive.

Run: python -m tests.sweep_context_smoke_server --port 56039
Stop with Ctrl-C. A complete logical SQLite dump must remain unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from radioinspector.session_service import SessionService
from tests.smoke_fixture_support import run_fixture


def _populate(service: SessionService) -> dict:
    base = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    for suffix in ('one', 'two'):
        session = service.create_session({
            'id': f'context-{suffix}', 'name': f'Synthetic context {suffix}',
            'streams': [
                {'id': f'spectrum-{suffix}', 'device': 'Synthetic tinySA',
                 'role': 'spectrum_analyzer', 'level_unit': 'dBm',
                 'capabilities': ['sweep', 'waterfall']},
                {'id': f'receiver-{suffix}', 'device': 'Synthetic AR-DV1',
                 'role': 'receiver', 'level_unit': 's_meter_count'},
            ],
        })
        service.start_session(session.id, at=base)
        for number in (7, 2, 5, 1, 6, 3, 4):
            levels = [-100 + i / 10 + number for i in range(101)]
            service.ingest_sweep({
                'id': f'{suffix}-sweep-{number}', 'session_id': session.id,
                'stream_id': f'spectrum-{suffix}',
                'interval': {
                    'started_at': (base + timedelta(seconds=number * 10, microseconds=1)).isoformat(),
                    'ended_at': (base + timedelta(seconds=number * 10 + 1, microseconds=2)).isoformat(),
                },
                'frequencies_hz': [100_000_000 + 100_000 * i for i in range(101)],
                'levels': levels, 'level_unit': 'dBm',
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
    service.create_sweep_reference('context-one', {
        'name': 'Synthetic retained reference', 'sweep_id': 'one-sweep-1',
    })
    return {
        'session': 'context-one', 'stream': 'spectrum-one', 'anchor': 'one-sweep-4',
        'before': 2, 'after': 2, 'points': 101,
        'expected_ids': [f'one-sweep-{n}' for n in range(2, 7)],
        'expected_offsets': [-2, -1, 0, 1, 2],
        'anchor_first_level_dbm': -96,
    }


def main() -> None:
    run_fixture(_populate, default_port=56039, prefix='openradio-sweep-context-', description=__doc__)


if __name__ == '__main__':
    main()
