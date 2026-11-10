"""Owned synthetic archive metadata shared by additive-migration regressions."""
import base64
import hashlib
import io
import json
import wave

from radioinspector.monitoring.store import ProjectStore
from radioinspector.contracts import to_primitive
from tests.test_platform import BASE


def add_current_metadata(service, session_id, stream_id):
    """Populate current-main domains without hardware, workers, or network I/O."""
    store = service._store
    projects = ProjectStore(store)
    created = to_primitive(BASE)
    project_id = 'synthetic-project'
    for kind, value in (
        ('project', dict(id=project_id, session_ids=[session_id], name='Synthetic migration',
                         state='disarmed', active_baseline_id='synthetic-baseline')),
        ('baseline', dict(id='synthetic-baseline', project_id=project_id, state='active',
                          session_id=session_id, source_sweep_ids=['sweep-0'],
                          level_unit='dBm', values=[-80, -70, -60, -50])),
        ('event', dict(id='synthetic-monitoring-event', project_id=project_id, state='open',
                       run_session_id=session_id, baseline_id='synthetic-baseline',
                       source_refs=[dict(session_id=session_id, sweep_id='sweep-0')],
                       last_seen=created, disposition='unreviewed')),
    ):
        projects.put(kind, dict(value, created_at=created))
    with store._connection:
        store._connection.execute('INSERT INTO monitoring_webhooks VALUES (?,?,?)',
                                  (project_id, 'https://example.invalid/synthetic', 'synthetic-secret'))
        store._connection.execute('INSERT INTO monitoring_notifications VALUES (?,?,?,?,?,?,?,?,?,?)',
                                  ('synthetic-notification', project_id, created,
                                   json.dumps({'event_id': 'synthetic-monitoring-event'}),
                                   'pending', 2, created, 'synthetic retry', created, None))
    # A real, tiny WAV payload verifies byte preservation beyond JSON measurements.
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\x01\x00\xff\x7f' * 4)
    raw = output.getvalue()
    artifact = service.save_instrument_artifact(session_id, stream_id, dict(
        kind='audio', mime_type='audio/wav', filename='synthetic.wav',
        data_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest(),
        metadata=dict(occurred_at=created, source='synthetic-migration')))
    return artifact


def current_metadata_values(connection):
    """Fixed current-main projections; every fixture row must survive exactly."""
    queries = {
        'monitoring_schema': 'SELECT * FROM monitoring_schema',
        'monitoring_records': 'SELECT * FROM monitoring_records ORDER BY kind,id',
        'monitoring_webhooks': 'SELECT * FROM monitoring_webhooks ORDER BY project_id',
        'monitoring_notifications': 'SELECT * FROM monitoring_notifications ORDER BY id',
        'monitoring_sessions': 'SELECT * FROM monitoring_sessions ORDER BY project_id,session_id',
        'monitoring_sweep_index': 'SELECT * FROM monitoring_sweep_index ORDER BY sweep_id',
        'instrument_artifacts': 'SELECT * FROM instrument_artifacts ORDER BY id',
    }
    return {key: [tuple(row) for row in connection.execute(query)] for key, query in queries.items()}
