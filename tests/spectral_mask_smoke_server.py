"""Synthetic spectral-mask fixture; never opens a user archive or device.

Run: python -m tests.spectral_mask_smoke_server --port 56036
Stop with Ctrl-C, or use --shutdown-stdin with a private parent pipe.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from radioinspector.session_service import SessionService
from tests.smoke_fixture_support import run_fixture


def _populate(service: SessionService) -> dict:
    base = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    for suffix in ('one', 'two'):
        session = service.create_session({
            'id': f'mask-{suffix}', 'name': f'Synthetic mask {suffix}',
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
            frequencies = [100_000_000 + 40_000 * i for i in range(101)]
            levels = [-95 + i / 10 for i in range(101)]
            levels[30] += 25
            levels[69] += 16
            levels[70] += 18
            levels[71] += 16
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
    service.create_sweep_reference('mask-one', {
        'name': 'Synthetic retained reference', 'sweep_id': 'one-sweep-1',
    })
    return {'session': 'mask-one', 'stream': 'spectrum-one', 'sweep': 'one-sweep-2'}


def main() -> None:
    run_fixture(_populate, default_port=56036, prefix='openradio-mask-smoke-', description=__doc__)


if __name__ == '__main__':
    main()
