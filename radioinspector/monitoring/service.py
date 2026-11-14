"""Projects bind sessions, receive profiles and operator intent durably."""
import json
import os
import statistics
import threading
from datetime import datetime, timezone
from uuid import uuid4

from ..contracts import DeviceRole, format_time, to_primitive, utc_now
from ..device_workers import SimulatedAdapter, WorkerError
from ..serial_instruments import ARDV1SerialAdapter, _rf_command, validate_windows_com_port
from ..tinysa_profile import validate_receive_settings
from .store import ProjectStore


from .models import record, text
from .baselines import BaselineOperations
from .guard import GuardOperations
from .research import ResearchOperations
from .operations import OperationalMonitoring
from .preparation import PreparationOperations


class MonitoringService(BaselineOperations, ResearchOperations, GuardOperations, OperationalMonitoring, PreparationOperations):
    def __init__(self, sessions, workers):
        self.sessions, self.workers = sessions, workers
        self.store = ProjectStore(sessions._store)
        self.lock = threading.RLock()
        self._closed = False
        self.preparing={}
        self.initialize_runtime()
        self.initialize_operations()

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

    def preflight(self, identifier, *, for_preparation=False):
        project = self.project(identifier)
        session = self.sessions.get_session(project['active_session_id'])
        analyzer = next(s for s in session.streams if s.id == project['analyzer_stream_id'])
        receiver = next(s for s in session.streams if s.id == project['receiver_stream_id'])
        checks = []
        def check(name, ok, detail):
            checks.append(dict(name=name, ok=bool(ok), detail=detail))
        if any(plan is not None for plan in (
                project.get('scan_plan'),
                project.get('profile', {}).get('analyzer', {}).get('settings', {}).get('scan_plan'),
                analyzer.settings.get('scan_plan'))):
            check('scan_plan_runtime', False,
                  'Сохранённый план поддиапазонов требует Rust-ядро; Python не запускает частичный обзор')
        adapters = (analyzer.settings.get('adapter'), receiver.settings.get('adapter'))
        check('adapters', adapters in (('tinysa-serial', 'ardv1-serial'), ('tinysa-sim', 'ardv1-sim')),
              'tinySA Ultra + AR-DV1; симуляторы выбираются только явно')
        for label, stream, default_baud in (('analyzer', analyzer, 9600), ('receiver', receiver, 115200)):
            try:
                # The base constructor only validates shared timing/retry
                # settings; it neither constructs a transport nor starts I/O.
                SimulatedAdapter(stream)
                check(label+'_worker_settings',True,'Период и параметры повторов совместимы с исполнителем')
            except (ValueError,TypeError,OverflowError) as exc:
                check(label+'_worker_settings',False,str(exc))
            if stream.settings.get('adapter') not in ('tinysa-serial', 'ardv1-serial'):
                continue
            valid_port=isinstance(stream.port,str) and bool(stream.port.strip()) and not stream.port.startswith('sim://')
            if valid_port and os.name=='nt':
                try:validate_windows_com_port(stream.port)
                except WorkerError:valid_port=False
            check(label + '_serial_port', valid_port,
                  'Нужен явно заданный последовательный порт; доступность без открытия не проверяется')
            try:
                baud = int(stream.settings.get('baud', default_baud))
                valid_baud = baud in (9600, 19200, 38400, 57600, 115200)
            except (ValueError, TypeError, OverflowError):
                valid_baud = False
            check(label + '_serial_baud', valid_baud, 'Скорости порта: 9600, 19200, 38400, 57600, 115200')
        if adapters[1] == 'ardv1-serial':
            try:
                # Share the adapter's command validation without constructing a
                # transport or claiming the connected receiver has been verified.
                _rf_command(receiver.settings.get('frequency_hz', 145_500_000))
                profile = receiver.settings.get('receiver_profile')
                if profile not in (None, 'ardv1-monitor-v1'):
                    raise ValueError('Неизвестный профиль AR-DV1')
                modes = ARDV1SerialAdapter.MONITOR_MODES if profile == 'ardv1-monitor-v1' else ('FM', 'AM')
                mode = str(receiver.settings.get('mode') or 'FM').strip().upper()
                if mode not in modes:
                    raise ValueError('Режим не поддерживается выбранным профилем AR-DV1')
                check('receiver_profile', True, 'Конфигурация AR-DV1 совместима с профилем; аппаратная проверка не выполнена')
            except (ValueError, TypeError, WorkerError) as exc:
                check('receiver_profile', False, str(exc))
        if adapters[0] == 'tinysa-serial':
            declared=project.get('preparation',{})
            check('operator_receive_path',declared.get('receive_path_confirmed') is True and declared.get('location_confirmed') is True,
                'Оператор подтверждает антенну, тракт и место; RF readback не заявлен')
            try:
                validate_receive_settings(analyzer.settings)
                check('receive_profile', True, 'Консервативный профиль приёма; RF off')
            except (ValueError, TypeError) as exc:
                check('receive_profile', False, str(exc))
            if project.get('baseline_id') and not for_preparation:
                try:
                    self.guard_acquisition_context(project)
                    check('baseline_acquisition_context',True,'Контекст RX эталона воспроизводим; аппаратная проверка не выполнена')
                except (ValueError,TypeError,KeyError) as exc:
                    check('baseline_acquisition_context',False,str(exc))
        if project.get('baseline_id') and not for_preparation:
            try:
                baseline=self.detection_baseline(self.store.get('baseline',project['baseline_id']))
                check('baseline_cadence',bool(baseline.get('sweep_period_seconds')),
                    'Период эталона известен; при недоступных источниках соберите новый эталон')
            except (ValueError,TypeError,KeyError) as exc:
                check('baseline_cadence',False,str(exc))
        capacity = self.sessions.archive_capacity()
        check('archive', capacity.get('state') not in ('full',),
              'Полный архив; автоматическое удаление выключено')
        sweeps = self.sessions.list_sweeps(session.id, stream_id=analyzer.id, limit=128)
        durations = [(s.interval.ended_at-s.interval.started_at).total_seconds() for s in sweeps]
        performance = dict(sweeps=len(sweeps), mean_sweep_seconds=statistics.mean(durations) if durations else None,
                           mean_sweep_bytes=statistics.mean(len(json.dumps(to_primitive(s)).encode()) for s in sweeps) if sweeps else None)
        storage = self.storage_report(identifier,probe=True) if self.sessions._store and self.sessions._store.persistent else None
        if storage:check('storage_budget',storage['ready'],'Свободное место, запись и прогноз хранения без автоудаления')
        return dict(project_id=identifier, ready=all(c['ok'] for c in checks), checks=checks,storage=storage,
                    baseline_collection=self.baseline_collection(project),
                    capacity=capacity, performance=performance,
                    workers=self.workers.list_workers(session.id), hardware_validation='not_run',
                    baseline_ready=project['baseline_id'] is not None)

    def close(self):
        with self.lock:
            if self._closed: return
            self._closed = True
            identifiers=list(self.runtime)
        self._supervisor_stop.set()
        if self._supervisor and self._supervisor is not threading.current_thread():self._supervisor.join(timeout=8)
        for identifier in identifiers:
            with self._project_lifecycle(identifier):
                runtime=self.runtime.get(identifier)
                if runtime is None:continue
                project=self.project(identifier)
                resume=project.get('autostart',False) and not runtime['stop'].is_set()
                self.disarm(identifier, {})
                if resume:
                    project=self.project(identifier);project['resume_requested']=True;self.store.put('project',project)
        for identifier in list(self.preparing):self.stop_preparation(identifier,{})
        self.workers.control_authorizer = None
        self.store.close()
