"""Core-owned guard and independent receiver handoff. No RF generation."""
import base64
import copy
import hashlib
import threading
import time
from uuid import uuid4

from ..contracts import format_time, parse_time, to_primitive, utc_now
from ..device_workers import WorkerError
from ..tinysa import audio
from .models import record


class GuardOperations:
    @staticmethod
    def receiver_target(event):
        frequency = event['center_frequency_hz']
        if not 100_000 <= frequency <= 1_300_000_000:
            raise ValueError('Событие вне диапазона AR-DV1')
        return ((frequency+5)//10)*10

    def initialize_runtime(self):
        self.runtime = {}
        self._project_lifecycles = {}
        self.workers.control_authorizer = self.authorize_control

    def _project_lifecycle(self, identifier):
        with self.lock:
            return self._project_lifecycles.setdefault(identifier, threading.RLock())

    def authorize_control(self, sid, stream, owner):
        with self.lock:
            if sid in self.preparing.values():raise WorkerError('Сбор эталона владеет приборами; сначала остановите сбор')
            for runtime in self.runtime.values():
                if (sid,stream) in runtime['claims']:
                    if runtime['stop'].is_set(): raise WorkerError('Охрана останавливается')
                    lease=runtime.get('manual',{}).get(stream)
                    if lease and lease['returning'] and owner==runtime['token']: return
                    if lease and (lease['returning'] or time.monotonic()>=lease['expires']):
                        raise WorkerError('Ручная аренда истекла/возвращается; автоматизация остаётся на паузе')
                    expected = lease['token'] if lease else runtime['token']
                    if owner != expected:
                        raise WorkerError('Прибор принадлежит охране: сначала явно возьмите ручное управление')

    def configure(self, identifier, payload):
        if set(payload) - {'audio', 'receiver_mode'}: raise ValueError('Неизвестная настройка охраны')
        with self.lock:
            project = self.project(identifier)
            if identifier in self.runtime: raise ValueError('Сначала снимите проект с охраны')
            if 'receiver_mode' in payload:
                from ..serial_instruments import ARDV1SerialAdapter
                if payload['receiver_mode'] not in ARDV1SerialAdapter.MONITOR_MODES:
                    raise ValueError('Режим недоступен; TETRA не входит в AUTO')
                project['receiver_mode'] = payload['receiver_mode']
            if 'audio' in payload:
                value = payload['audio']
                if value is None: project['audio'] = None
                else:
                    if not isinstance(value, dict) or set(value) != {'device','name','confirm'} or value['confirm'] is not True:
                        raise ValueError('Выберите и подтвердите аналоговый аудиовход AR-DV1')
                    match = next((d for d in audio.inputs()['devices'] if d['index'] == value['device'] and d['name'] == value['name']), None)
                    if match is None: raise ValueError('Выбранный аудиовход отсутствует')
                    project['audio'] = dict(device=match['index'], name=match['name'])
            return self.store.put('project', project)

    def arm(self, identifier, payload):
        with self._project_lifecycle(identifier):
            return self._arm(identifier,payload)

    def _arm(self, identifier, payload):
        if payload: raise ValueError('Постановка на охрану принимает пустой объект')
        with self.lock:
            if self._closed: raise WorkerError('Служба мониторинга закрывается')
            project = self.project(identifier)
            if identifier in self.runtime: return project
            if not project.get('baseline_id') or not self.preflight(identifier)['ready']:
                raise ValueError('Требуются подтверждённый эталон и успешная подготовка')
            if self.sessions._store is None or not self.sessions._store.persistent:
                raise ValueError('Автономная охрана требует сохраняемый SQLite-архив')
            old_sid = project['active_session_id']
            if any(r['sid'] == old_sid for r in self.runtime.values()):
                raise WorkerError('Этот сеанс уже принадлежит другому проекту на охране')
            self.workers.stop_session(old_sid)
            self.preparing.pop(identifier,None)
            if self.sessions.get_session(old_sid).state.value == 'running':
                self.sessions.stop_session(old_sid)
            streams = []
            for role in ('analyzer', 'receiver'):
                stream = copy.deepcopy(project['profile'][role])
                for key in ('id', 'session_id', 'state'): stream.pop(key, None)
                if role == 'receiver':
                    stream['settings'].update(receiver_profile='ardv1-monitor-v1', mode=project.get('receiver_mode', 'AUTO'))
                streams.append(stream)
            session = self.sessions.create_session(dict(name=project['name'], streams=streams, metadata={'monitoring_project_id':identifier}))
            self.sessions.start_session(session.id)
            project.update(active_session_id=session.id, session_ids=(project['session_ids']+[session.id])[-128:],
                analyzer_stream_id=session.streams[0].id, receiver_stream_id=session.streams[1].id,
                state='armed', detector_state=None, detector_cursor=None, detector_rowid=0,
                last_error=None, coverage='wide', armed_at=format_time(utc_now()))
            runtime = dict(token=uuid4().hex, sid=session.id, streams=[s.id for s in session.streams],
                claims={(session.id,s.id) for s in session.streams},
                stop=threading.Event(), threads=[], manual={}, receiver_busy=False, epoch=0)
            self.runtime[identifier] = runtime
            self.store.put('project', project)
            self._launch_guard(identifier, runtime)
            return project

    def _launch_guard(self, identifier, runtime):
        thread = threading.Thread(target=self._guard_loop, args=(identifier,runtime), daemon=True,
                                  name='monitor-'+identifier[:8])
        runtime['threads'].append(thread); thread.start()

    def disarm(self, identifier, payload):
        with self._project_lifecycle(identifier):
            return self._disarm(identifier,payload)

    def _disarm(self, identifier, payload):
        if payload: raise ValueError('Снятие с охраны принимает пустой объект')
        if identifier in self.preparing:self.stop_preparation(identifier,{})
        with self.lock:
            runtime = self.runtime.get(identifier)
            if runtime: runtime['stop'].set(); runtime['epoch'] += 1
            project = self.project(identifier)
        if runtime:
            for thread in list(runtime['threads']):
                if thread is not threading.current_thread(): thread.join(timeout=15)
            for sid in {sid for sid,_ in runtime['claims']}:
                self.workers.stop_session(sid)
        with self.lock:
            self.runtime.pop(identifier, None)
            project = self.project(identifier)
            project.update(state='ready', coverage='stopped', resume_requested=False)
            return self.store.put('project', project)

    def _guard_loop(self, identifier, runtime):
        try:
            statuses = self.workers.start_session(runtime['sid'])
            if any(x['state'] == 'error' for x in statuses):
                raise WorkerError('Не удалось запустить оба прибора; проверьте журнал исполнителей')
            storage_checked=0.
            while not runtime['stop'].wait(.2):
                project = self.project(identifier)
                if time.monotonic()-storage_checked>=2:
                    if not self.storage_report(identifier,probe=True)['ready']:
                        raise WorkerError('Архив не обеспечивает резерв места/запись/заданный срок хранения')
                    storage_checked=time.monotonic()
                statuses=self.workers.list_workers(runtime['sid'])
                self._device_health(identifier,runtime,statuses)
                project=self.project(identifier)
                analyzer = project['analyzer_stream_id']
                if analyzer not in runtime['manual']:
                    self.analyze(identifier)
                self._search_tick(identifier,runtime)
                self._queue_receiver(identifier, runtime)
        except Exception as exc:
            with self.lock:
                project = self.project(identifier)
                project.update(state='error', last_error=str(exc), coverage='error')
                runtime['critical_alert']=self.volatile_error(project,str(exc))
                try:
                    self.store.put('project', project)
                    self.notify_error(project,str(exc))
                    runtime['critical_alert']['volatile']=False
                except Exception: pass  # Archive failure must remain visible in runtime too.
                runtime['error'] = str(exc)
            runtime['stop'].set()
            for sid in {sid for sid,_ in runtime['claims']}:
                try: self.workers.stop_session(sid)
                except Exception: pass

    def _device_health(self,identifier,runtime,statuses):
        with self.lock:
            failed=[x for x in statuses if not x['alive'] and x['stream_id'] not in runtime['manual']]
            previous=runtime.get('device_faults',{})
            faults={x['stream_id']:x.get('started_at') for x in failed}
            project=self.project(identifier)
            if failed:
                runtime['warning']='Часть приборов недоступна; исправный прибор продолжает регистрацию'
                analyzer_ok=not any(x['stream_id']==project['analyzer_stream_id'] for x in failed)
                project.update(state='degraded',last_error=runtime['warning'])
                if not analyzer_ok:project['coverage']='unavailable'
                for worker in failed:
                    if worker['stream_id'] not in previous or previous[worker['stream_id']]!=worker.get('started_at'):
                        self._notify(project,worker['stream_id']+':'+str(worker.get('started_at')),'device_unavailable',
                            'critical','Прибор недоступен: '+worker['device'],None)
            elif previous:
                runtime.pop('warning',None)
                project.update(state='searching' if runtime.get('search') else 'armed',last_error=None)
                if project.get('coverage')=='unavailable':project['coverage']='wide'
            if faults!=previous:
                runtime['device_faults']=faults;self.store.put('project',project)

    def _queue_receiver(self, identifier, runtime):
        with self.lock:
            project = self.project(identifier)
            if runtime['receiver_busy'] or project['receiver_stream_id'] in runtime['manual']: return
            if project['receiver_stream_id'] in runtime.get('device_faults',{}):return
            events = self.store.pending_receiver_events(identifier, runtime['sid'])
            if not events: return
            event = max(events, key=lambda x:(x['last_seen'], x['id']))
            try: self.receiver_target(event)
            except ValueError as exc:
                event['analysis'] = dict(state='unsupported_frequency', reason=str(exc))
                self.store.put('event',event); return
            age = (utc_now()-parse_time(event['last_seen'])).total_seconds()
            if age > 30:
                event['analysis'] = {'state':'expired','reason':'Сигнал исчез до перестройки; обнаружение сохранено'}
                self.store.put('event', event); return
            event['analysis'] = dict(state='queued', requested_mode=project.get('receiver_mode','AUTO'))
            self.store.put('event', event)
            runtime['receiver_busy'] = True
            thread = threading.Thread(target=self._investigate_event,args=(identifier,event,runtime,runtime['epoch']),daemon=True)
            runtime['threads'] = [t for t in runtime['threads'] if t.is_alive()]
            runtime['threads'].append(thread); thread.start()

    def _investigate_event(self, identifier, event, runtime, epoch):
        project = self.project(identifier); sid = runtime['sid']; stream = project['receiver_stream_id']
        target_frequency = self.receiver_target(event)
        investigation = None; audio_error = None; artifact = None
        def check():
            if runtime['stop'].is_set() or runtime['epoch'] != epoch or stream in runtime['manual']:
                raise WorkerError('Исследование прервано сменой владельца/остановкой охраны')
        try:
            check()
            candidate = self.sessions.create_candidate(sid, dict(source_stream_id=project['analyzer_stream_id'],
                interval=dict(started_at=event['first_seen'],ended_at=event['last_seen']),
                center_frequency_hz=event['center_frequency_hz'], peak_level=event['peak_level'], level_unit='dBm',
                observation_ids=[r['sweep_id'] for r in event['source_refs'] if r['session_id']==sid]))
            investigation = self.sessions.enqueue_investigation(sid,candidate.id,
                dict(target_stream_id=stream,target_frequency_hz=target_frequency,mode=project.get('receiver_mode','AUTO'),priority=1))
            def tune(sid, stream, payload):
                check(); return self.workers.tune(sid,stream,payload,control_owner=runtime['token'])
            investigation = self.sessions.start_investigation(sid,investigation.id,tune=tune)
            if investigation.state.value != 'listening': raise WorkerError(investigation.error or 'Настройка не подтверждена')
            tuned_at = utc_now(); duration = project['parameters']['dwell_seconds']
            configured = project.get('audio')
            deadline = time.monotonic()+duration
            if configured:
                try:
                    if not any(d['index']==configured['device'] and d['name']==configured['name'] for d in audio.inputs()['devices']):
                        raise ValueError('Назначенный аудиовход исчез или изменился')
                    raw = audio.capture(dict(device=configured['device'],seconds=duration,sample_rate_hz=24000),check=check)
                    check()
                    artifact = self.sessions.save_instrument_artifact(sid,stream,dict(kind='audio',mime_type='audio/wav',
                        filename='ardv1-event.wav',sha256=hashlib.sha256(raw).hexdigest(),data_base64=base64.b64encode(raw).decode(),
                        metadata=dict(occurred_at=format_time(tuned_at),ended_at=format_time(utc_now()),
                            event_id=event['id'],investigation_id=investigation.id,frequency_hz=target_frequency,
                            detected_frequency_hz=event['center_frequency_hz'],
                            requested_mode=project.get('receiver_mode','AUTO'),source='core_analog_input',device_name=configured['name'])))
                except Exception as exc: audio_error = str(exc)
            else: audio_error = 'Аудиоисточник не выбран оператором'
            while time.monotonic() < deadline:
                check(); runtime['stop'].wait(min(.05, max(0,deadline-time.monotonic())))
            check()
            observations = [o for o in self.sessions.list_observations(sid,stream_id=stream,limit=1000)
                if o.frequency_hz==target_frequency and o.mode==project.get('receiver_mode','AUTO') and o.interval.started_at>=tuned_at]
            if observations:
                self.sessions.complete_investigation(sid,investigation.id)
                state = 'observed' if any(o.squelch_open for o in observations) else 'not_present_at_receiver'
            else:
                self.sessions.fail_investigation(sid,investigation.id,'Нет наблюдений после подтверждённой настройки')
                state = 'no_observations'
            result = dict(state=state,investigation_id=investigation.id,requested_mode=project.get('receiver_mode','AUTO'),
                target_frequency_hz=target_frequency,detected_frequency_hz=event['center_frequency_hz'],
                tuned_at=format_time(tuned_at),ended_at=format_time(utc_now()),audio=artifact,audio_error=audio_error,
                observation_ids=[o.id for o in observations],receiver_status=[to_primitive(o) for o in observations[-10:]],
                classifications=[dict(mode=o.decoded_mode,observation_id=o.id,source=o.metadata.get('decoder_source','simulated')) for o in observations if o.decoded_mode])
        except Exception as exc:
            result = dict(state='interrupted' if runtime['stop'].is_set() or runtime['epoch']!=epoch else 'failed',
                          error=str(exc),investigation_id=investigation.id if investigation else None)
            if investigation:
                try: self.sessions.fail_investigation(sid,investigation.id,str(exc))
                except ValueError: pass
        finally:
            with self.lock:
                current = self.store.get('event',event['id']); current['analysis'] = result
                self.store.put('event',current); runtime['receiver_busy'] = False

    def guard_status(self, identifier):
        project = self.project(identifier)
        with self.lock:
            runtime = self.runtime.get(identifier)
            if runtime and runtime.get('error'):
                project.update(state='error',coverage='error',last_error=runtime['error'])
            return dict(project=project, active=bool(runtime and not runtime['stop'].is_set()),
                receiver_busy=bool(runtime and runtime['receiver_busy']),
                error=(runtime.get('error') or runtime.get('warning')) if runtime else project.get('last_error'),
                manual=[dict(role=lease['role'],expired=time.monotonic()>=lease['expires'],returning=lease['returning'],
                             expires_seconds=max(0,round(lease['expires']-time.monotonic()))) for lease in runtime['manual'].values()] if runtime else [],
                workers=self.workers.list_workers(project['active_session_id']),
                audio_configured=bool(project.get('audio')), hardware_validation='not_run')
