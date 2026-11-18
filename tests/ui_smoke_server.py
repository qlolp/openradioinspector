"""Read-only native UI fixture: synthetic sources, no USB or user archive."""
from datetime import datetime, timedelta, timezone

from tests.smoke_fixture_support import run_fixture
from radioinspector.contracts import to_primitive
from radioinspector.monitoring.store import ProjectStore


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
        # Seed before the fixture takes its read-only archive digest. No worker,
        # guard, audio source or physical port is started by this scenario.
        project_id = 'ui-project-' + suffix
        created = base.isoformat()
        projects = ProjectStore(service._store)
        streams = {stream.role.value: to_primitive(stream) for stream in session.streams}
        projects.put('project', dict(
            id=project_id, project_id=project_id, created_at=created,
            name='UI project ' + suffix, site='Synthetic UI fixture', state='draft',
            session_ids=[session.id], active_session_id=session.id,
            analyzer_stream_id='ui-spectrum-' + suffix, receiver_stream_id='ui-receiver-' + suffix,
            baseline_id=None, autostart=False,
            parameters=dict(margin_db=6., impulse_db=12., hysteresis_db=3.,
                            confirmations=3, window_sweeps=5, dwell_seconds=10.),
            profile=dict(analyzer=streams['spectrum_analyzer'], receiver=streams['receiver'])))
        projects.put('event', dict(
            id='ui-event-' + suffix, project_id=project_id, created_at=created,
            run_session_id=session.id, kind='persistent', state='closed',
            first_seen=created, last_seen=created, disposition='unreviewed', operator_comment='',
            start_frequency_hz=100_000_000, end_frequency_hz=100_200_000,
            center_frequency_hz=100_100_000, peak_level=-51., peak_excess_db=18., source_count=1,
            analysis=dict(state='no_observations'), source_refs=[dict(
                session_id=session.id, stream_id='ui-spectrum-' + suffix,
                sweep_id=f'ui-{suffix}-sweep-1')]))
    projects = ProjectStore(service._store)
    first = projects.get('project', 'ui-project-one')
    first['session_ids'].append('ui-two')
    projects.put('project', first)
    older = projects.get('event', 'ui-event-one')
    older.update(id='ui-event-older', run_session_id='ui-two',
                 created_at=(base - timedelta(seconds=10)).isoformat(),
                 first_seen=(base - timedelta(seconds=10)).isoformat(),
                 last_seen=(base - timedelta(seconds=10)).isoformat(),
                 source_refs=[dict(session_id='ui-two', stream_id='ui-spectrum-two', sweep_id='ui-two-sweep-1')])
    projects.put('event', older)
    return {'session': 'ui-one', 'synthetic': True, 'hardware_opened': False}


if __name__ == '__main__':
    run_fixture(_populate, default_port=0, prefix='ori-native-ui-', description=__doc__)
