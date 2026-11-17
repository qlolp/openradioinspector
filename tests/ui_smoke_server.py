"""Read-only native UI fixture: synthetic sources, no USB or user archive."""
from datetime import datetime, timedelta, timezone

from tests.smoke_fixture_support import run_fixture


def _populate(service):
    base = datetime.now(timezone.utc) - timedelta(seconds=30)
    for suffix in ('one', 'two'):
        session = service.create_session({
            'id': 'ui-' + suffix, 'name': 'UI fixture ' + suffix,
            'streams': [
                {'id': 'ui-spectrum-' + suffix, 'device': 'Synthetic tinySA',
                 'role': 'spectrum_analyzer', 'level_unit': 'dBm'},
                {'id': 'ui-receiver-' + suffix, 'device': 'Synthetic AR-DV1',
                 'role': 'receiver', 'level_unit': 's_meter_count'},
            ],
        })
        service.start_session(session.id, at=base)
        for index in range(1, 4):
            service.ingest_sweep({
                'id': f'ui-{suffix}-sweep-{index}', 'session_id': session.id,
                'stream_id': 'ui-spectrum-' + suffix,
                'interval': {'started_at': (base + timedelta(seconds=index)).isoformat(),
                             'ended_at': (base + timedelta(seconds=index + .1)).isoformat()},
                'frequencies_hz': [100_000_000, 100_100_000, 100_200_000],
                'levels': [-100, -50 - index, -0.0], 'level_unit': 'dBm',
                'settings': {'adapter': 'synthetic-ui-fixture', 'simulated': True,
                             'generator_output': 'off'},
            })
    return {'session': 'ui-one', 'synthetic': True, 'hardware_opened': False}


if __name__ == '__main__':
    run_fixture(_populate, default_port=0, prefix='ori-native-ui-', description=__doc__)
