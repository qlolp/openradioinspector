"""Synthetic-only three-threshold HTTP fixture; never opens a user archive.

Run: python -m tests.three_thresholds_smoke_server --port 56038
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
            'id': f'three-{suffix}', 'name': f'Synthetic thresholds {suffix}',
            'streams': [
                {'id': f'spectrum-{suffix}', 'device': 'Synthetic tinySA',
                 'role': 'spectrum_analyzer', 'level_unit': 'dBm',
                 'capabilities': ['sweep', 'waterfall']},
                {'id': f'receiver-{suffix}', 'device': 'Synthetic AR-DV1',
                 'role': 'receiver', 'level_unit': 's_meter_count'},
            ],
        })
        service.start_session(session.id, at=base)
        for number in (1, 2):
            levels = [-95.0] * 101
            levels[30], levels[69], levels[70], levels[71] = -65, -85, -75, -85
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
    service.create_sweep_reference('three-one', {
        'name': 'Synthetic retained reference', 'sweep_id': 'one-sweep-1',
    })
    return {
        'session': 'three-one', 'stream': 'spectrum-one', 'sweep': 'one-sweep-2',
        'levels_dbm': [-90, -80, -70], 'points': 101,
        'exceedance_points': [4, 2, 1], 'region_counts': [2, 2, 1],
        'peak_index': 30, 'peak_frequency_hz': 103_000_000, 'peak_excess_db': [25, 15, 5],
    }


def main() -> None:
    run_fixture(_populate, default_port=56038, prefix='openradio-three-thresholds-', description=__doc__)


if __name__ == '__main__':
    main()
