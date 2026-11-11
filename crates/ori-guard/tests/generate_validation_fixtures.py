"""Actual Python monitoring API/error oracle over owned SQLite, without HTTP/I/O."""
from __future__ import annotations

import os
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "ARROW_NUM_THREADS"):
    os.environ[_name] = "1"

import argparse
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from radioinspector.contracts import Sweep, to_primitive
from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.monitoring.api import dispatch
from radioinspector.monitoring.service import MonitoringService
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore

PROJECT = 'project-é/%+?"'
MISSING = 'project-é/%+?"'
OTHER = 'other-project/%+?'
EVENT = 'event-é/%+?"'
MISSING_EVENT = 'event-é/%+?"'
FOREIGN = 'other-event/%+?'


def cases():
    result = []
    def add(operation, payload=None, project=MISSING, event=EVENT, state='draft', full=False):
        result.append(dict(case_id=f'{len(result):03d}-{operation}', operation=operation,
            payload={} if payload is None else payload, project_id=project, event_id=event,
            project_state=state, catalog_full=full))
    add('project')
    for operation in ('create_baseline', 'add_allowed', 'review_event'):
        for payload in ({}, {'unexpected': True}, {'confirm': True, 'name': 'Quiet', 'location': 'Roof'}, {'disposition': 'authorized'}):
            add(operation, payload)
    for payload in ({}, {'disposition': 'invalid'}, {'unexpected': True}):
        add('review_event', payload, PROJECT, MISSING_EVENT)
        add('review_event', payload, PROJECT, FOREIGN)
        add('review_event', payload, PROJECT, EVENT)
    add('get_event', project=PROJECT, event=MISSING_EVENT)
    add('get_event', project=PROJECT, event=FOREIGN)
    add('get_event', project=MISSING, event=FOREIGN)
    add('get_event', project=PROJECT, event=EVENT)
    for operation in ('event_sources', 'event_audio', 'history_original', 'acknowledge_alert'):
        add(operation)
        add(operation, project=PROJECT, event=MISSING_EVENT)
        add(operation, project=PROJECT, event=FOREIGN)
        if operation in ('event_sources', 'event_audio'):
            add(operation, project=PROJECT, event=EVENT)
            add(operation, project=MISSING, event=FOREIGN)
    add('acknowledge_alert', {'unexpected': True})
    for payload in ({}, {'confirm': True}, {'confirm': True, 'name': '', 'location': ''},
                    {'confirm': True, 'name': 'Quiet', 'location': ''}, {'confirm': True, 'extra': 1}):
        add('create_baseline', payload, PROJECT)
    add('create_baseline', {'confirm': True, 'name': '', 'location': ''}, PROJECT, state='armed')
    for payload in ({}, {'unexpected': True}, {'name': 'Rule', 'start_hz': True, 'end_hz': 2},
                    {'name': 'Rule', 'start_hz': 1.0, 'end_hz': 2}, {'name': 'Rule', 'start_hz': -1, 'end_hz': 2},
                    {'name': 'Rule', 'start_hz': 3, 'end_hz': 2}, {'name': 'Rule', 'start_hz': 0, 'end_hz': 20_000_000_001},
                    {'name': 'Rule', 'start_hz': 1, 'end_hz': 2, 'watch_changes': 1},
                    {'name': '', 'start_hz': 1, 'end_hz': 2}):
        add('add_allowed', payload, PROJECT)
    for project in (MISSING, PROJECT):
        for operation in ('save_preparation', 'manual_acquire', 'manual_renew', 'manual_return', 'manual_tune', 'search_start', 'search_mark'):
            add(operation, project=project)
        add('manual_acquire', {'role': 'receiver'}, project)
        add('manual_renew', {'token': 'opaque-unrecognized'}, project)
        add('manual_return', {'token': None}, project)
        add('manual_tune', {'token': 'opaque-unrecognized', 'frequency_hz': 'invalid', 'mode': 'TETRA'}, project)
        add('search_start', {'token': 'opaque-unrecognized', 'event_id': MISSING_EVENT, 'span_hz': 'invalid'}, project)
        add('search_mark', {'token': 'opaque-unrecognized', 'label': ''}, project)
    add('save_preparation', {'antenna_notes': '', 'receive_path_confirmed': 1, 'location_confirmed': False}, PROJECT)
    add('save_preparation', {'antenna_notes': 'RX', 'receive_path_confirmed': True, 'location_confirmed': True})
    add('review_event', {'disposition': 'authorized', 'comment': {'é': -0.0, 'é': 2**80}}, PROJECT)
    add('add_allowed', {'name': '', 'start_hz': True, 'end_hz': 2}, PROJECT, full=True)
    add('add_allowed', {'unexpected': True}, PROJECT, full=True)
    return result


class Capture:
    def __init__(self, monitoring):
        self.monitoring = monitoring
        self.response = None
    def _send_json(self, value, status=200):
        self.response = dict(status=status, body=to_primitive(value))
    def _send_api_error(self, status, code, message):
        self._send_json({'contract': 'openradioinspector-platform', 'contract_version': 1,
                         'error': {'code': code, 'message': message}}, status)


def route(case):
    base = '/api/v1/monitoring-projects/' + quote(case['project_id'], safe='')
    operation = case['operation']
    if operation == 'project':
        return 'GET', base
    if operation in ('get_event', 'review_event', 'event_sources', 'event_audio'):
        suffix = {'get_event': '', 'review_event': '/review', 'event_sources': '/sources', 'event_audio': '/audio'}[operation]
        return ('POST' if operation == 'review_event' else 'GET'), base + '/events/' + quote(case['event_id'], safe='') + suffix
    if operation == 'history_original':
        return 'GET', base + '/history/' + quote(case['event_id'], safe='')
    if operation == 'acknowledge_alert':
        return 'POST', base + '/alerts/' + quote(case['event_id'], safe='') + '/ack'
    suffix = {'create_baseline': 'baselines', 'add_allowed': 'allowed', 'save_preparation': 'preparation',
              'manual_acquire': 'manual/acquire', 'manual_renew': 'manual/renew', 'manual_return': 'manual/return',
              'manual_tune': 'manual/tune', 'search_start': 'search/start', 'search_mark': 'search/mark'}[operation]
    return 'POST', base + '/' + suffix


def emit():
    base = json.loads((ROOT / 'contracts/fixtures/guard-domain-v1.json').read_text())
    stamp = datetime.fromisoformat(base['clock'].replace('Z', '+00:00'))
    primary = copy.deepcopy(base['created'])
    primary.update(id=PROJECT, project_id=PROJECT)
    other = dict(primary, id=OTHER, project_id=OTHER)
    event = copy.deepcopy(base['first_event'])
    event.update(id=EVENT, project_id=PROJECT, disposition='unreviewed', operator_comment='',
                 opaque={'é': -0.0, 'é': -1.0000000000000002, 'large': 2**80,
                         'reserved': {'$serde_json::private::Number': 'source'}})
    foreign = dict(event, id=FOREIGN, project_id=OTHER)
    records = dict(project=[primary, other], event=[event, foreign])
    output = dict(fixture_version=1, synthetic=True, hardware_validation='not_run',
                  records=records, cases=[])
    with tempfile.TemporaryDirectory(prefix='ori-guard-validation-') as directory:
        sessions = SessionService(store=SQLiteSessionStore(str(Path(directory) / 'archive.sqlite')), clock=lambda: stamp)
        workers = DeviceWorkerManager(sessions)
        monitoring = MonitoringService(sessions, workers)
        try:
            sessions.create_session({k: base['session'][k] for k in ('id', 'name', 'streams')})
            for sweep in base['sweeps'][:20]:
                sessions.ingest_sweep(Sweep.from_dict(sweep))
            for kind, values in records.items():
                for value in values:
                    monitoring.store.put(kind, value)
            catalog_full = False
            for case in cases():
                # Exercise the same serialized request key order stored in the
                # golden; Python str(dict) in an operator comment observes it.
                case = json.loads(json.dumps(case, ensure_ascii=False, sort_keys=True))
                monitoring.store.put('project', dict(primary, state=case['project_state']))
                monitoring.store.put('event', event)
                if case['catalog_full'] and not catalog_full:
                    with monitoring.store.transaction():
                        for index in range(1000):
                            monitoring.store.put('allowed', dict(id=f'full-{index:04d}', project_id=PROJECT,
                                created_at=base['clock'], name='Rule', start_hz=0, end_hz=0, comment='', watch_changes=False))
                    catalog_full = True
                before = '\n'.join(monitoring.store.db.iterdump())
                changes = monitoring.store.db.total_changes
                capture = Capture(monitoring)
                method, path = route(case)
                # Any accidental effect is a failing oracle, never simulated success.
                with patch.object(workers, 'start_session', side_effect=AssertionError('Unexpected worker start')), \
                     patch.object(workers, 'start_stream', side_effect=AssertionError('Unexpected worker start')), \
                     patch.object(workers, 'stop_session', side_effect=AssertionError('Unexpected worker stop')), \
                     patch.object(workers, 'stop_stream', side_effect=AssertionError('Unexpected worker stop')), \
                     patch.object(workers, 'tune', side_effect=AssertionError('Unexpected tune')):
                    assert dispatch(capture, method, path, case['payload'], {})
                assert capture.response is not None
                unchanged = before == '\n'.join(monitoring.store.db.iterdump())
                if capture.response['status'] >= 400:
                    assert unchanged and monitoring.store.db.total_changes == changes, case
                output['cases'].append(dict(case, expected=capture.response, unchanged=unchanged))
        finally:
            monitoring.close()
            workers.close()
            sessions.close()
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    destination = ROOT / 'contracts/fixtures/guard-validation-v1.json'
    expected = (json.dumps(emit(), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    if args.check:
        if not destination.is_file() or destination.read_bytes() != expected:
            parser.exit(1, 'Stale guard validation fixture\n')
    else:
        destination.write_bytes(expected)
    print(hashlib.sha256(expected).hexdigest())
