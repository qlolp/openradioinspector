"""Synthetic-only candidate-frequency-rounding HTTP fixture; never opens a user archive.

Run: python -m tests.frequency_rounding_smoke_server --port 56040
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
            'id': f'rounding-{suffix}', 'name': f'Synthetic rounding {suffix}',
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
        for label, frequency in (('rounded', 103_025_123), ('unchanged', 103_000_000),
                ('tolerance', 103_050_000), ('outside', 120_000_000),
                ('receiver', 145_506_250), ('upper', 103_075_000)):
            receiver = label == 'receiver'
            service.create_candidate(session.id, {
                'id': f'{suffix}-{label}',
                'source_stream_id': f'receiver-{suffix}' if receiver else f'spectrum-{suffix}',
                'interval': {
                    'started_at': (base + timedelta(microseconds=1)).isoformat(),
                    'ended_at': (base + timedelta(seconds=1, microseconds=2)).isoformat(),
                },
                'center_frequency_hz': frequency, 'bandwidth_hz': 10_000,
                'level_unit': 's_meter_count' if receiver else 'dBm',
                'peak_level': 37 if receiver else -65, 'score': 5,
            })
    service.create_sweep_reference('rounding-one', {
        'name': 'Synthetic retained reference', 'sweep_id': 'one-sweep-1',
    })
    return {
        'session': 'rounding-one', 'candidate': 'one-rounded',
        'expected_original_hz': 103_025_123, 'expected_rounded_hz': 103_000_000,
        'expected_offset_hz': -25_123,
        'profile': 'docs/examples/frequency-rounding-profile.json',
    }


def main() -> None:
    run_fixture(_populate, default_port=56040, prefix='openradio-frequency-rounding-', description=__doc__)


if __name__ == '__main__':
    main()
