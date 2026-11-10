"""Projects bind sessions, receive profiles and operator intent durably."""
import json
import statistics
import threading
from datetime import datetime, timezone
from uuid import uuid4

from ..contracts import DeviceRole, format_time, to_primitive, utc_now
from ..tinysa_profile import validate_receive_settings
from .store import ProjectStore


from .models import record, text
from .baselines import BaselineOperations
from .guard import GuardOperations
from .research import ResearchOperations


class MonitoringService(BaselineOperations, ResearchOperations, GuardOperations):
    def __init__(self, sessions, workers):
        self.sessions, self.workers = sessions, workers
        self.store = ProjectStore(sessions._store)
        self.lock = threading.RLock()
        self._closed = False
        self.initialize_runtime()

    def create(self, payload):
        if set(payload) - {'name', 'site', 'session_id'}:
            raise ValueError('Неизвестное поле проекта')
        session = self.sessions.get_session(text(payload.get('session_id'), 'session_id'))
        analyzers = [s for s in session.streams if s.role is DeviceRole.SPECTRUM_ANALYZER]
        receivers = [s for s in session.streams if s.role is DeviceRole.RECEIVER]
        if len(analyzers) != 1 or len(receivers) != 1:
            raise ValueError('Нужны ровно один анализатор и один receiver')
        analyzer, receiver = analyzers[0], receivers[0]
        if analyzer.level_unit != 'dBm' or receiver.level_unit != 's_meter_count':
            raise ValueError('Требуются исходные dBm и s_meter_count без преобразования')
        value = record(name=text(payload.get('name'), 'Название'),
                       site=text(payload.get('site'), 'Объект'), state='draft',
                       session_ids=[session.id], active_session_id=session.id,
                       analyzer_stream_id=analyzer.id, receiver_stream_id=receiver.id,
                       baseline_id=None, autostart=False, parameters=dict(
                           margin_db=6., impulse_db=12., hysteresis_db=3.,
                           confirmations=3, window_sweeps=5, dwell_seconds=10.),
                       profile=dict(analyzer=to_primitive(analyzer), receiver=to_primitive(receiver)))
        value['project_id'] = value['id']
        with self.lock:
            return self.store.put('project', value)

    def project(self, identifier):
        return self.store.get('project', identifier)

    def preflight(self, identifier):
        project = self.project(identifier)
        session = self.sessions.get_session(project['active_session_id'])
        analyzer = next(s for s in session.streams if s.id == project['analyzer_stream_id'])
        receiver = next(s for s in session.streams if s.id == project['receiver_stream_id'])
        checks = []
        def check(name, ok, detail):
            checks.append(dict(name=name, ok=bool(ok), detail=detail))
        adapters = (analyzer.settings.get('adapter'), receiver.settings.get('adapter'))
        check('adapters', adapters in (('tinysa-serial', 'ardv1-serial'), ('tinysa-sim', 'ardv1-sim')),
              'tinySA Ultra + AR-DV1; симуляторы выбираются только явно')
        if adapters[0] == 'tinysa-serial':
            try:
                validate_receive_settings(analyzer.settings)
                check('receive_profile', True, 'Консервативный профиль приёма; RF off')
            except (ValueError, TypeError) as exc:
                check('receive_profile', False, str(exc))
        capacity = self.sessions.archive_capacity()
        check('archive', capacity.get('state') not in ('full',),
              'Полный архив; автоматическое удаление выключено')
        sweeps = self.sessions.list_sweeps(session.id, stream_id=analyzer.id, limit=128)
        durations = [(s.interval.ended_at-s.interval.started_at).total_seconds() for s in sweeps]
        performance = dict(sweeps=len(sweeps), mean_sweep_seconds=statistics.mean(durations) if durations else None,
                           mean_sweep_bytes=statistics.mean(len(json.dumps(to_primitive(s)).encode()) for s in sweeps) if sweeps else None)
        return dict(project_id=identifier, ready=all(c['ok'] for c in checks), checks=checks,
                    capacity=capacity, performance=performance,
                    workers=self.workers.list_workers(session.id), hardware_validation='not_run',
                    baseline_ready=project['baseline_id'] is not None)

    def close(self):
        with self.lock:
            if self._closed: return
            self._closed = True
            identifiers=list(self.runtime)
        for identifier in identifiers:
            self.disarm(identifier, {})
        self.workers.control_authorizer = None
        self.store.close()
