"""Scoped research queries and explicit manual ownership of guard devices."""
import base64
import copy
import json
import threading
import time
from functools import wraps
from uuid import uuid4

from ..contracts import format_time, parse_time, to_primitive, utc_now
from ..device_workers import WorkerError
from ..trace import Trace
from ..session_store import _archive_time
from ..tinysa_profile import validate_receive_settings
from .detector import fingerprint
from .models import record, text


def project_action(method):
    @wraps(method)
    def run(self, identifier, payload):
        with self._project_lifecycle(identifier):
            return method(self,identifier,payload)
    return run


class ResearchOperations:
    def _lease(self, identifier, token, *, current=True):
        runtime = self.runtime.get(identifier)
        if not runtime or runtime['stop'].is_set(): raise WorkerError('Охрана не активна; снимите её для сброса владения')
        for stream, lease in runtime['manual'].items():
            if lease['token'] == token:
                if current and (lease['returning'] or time.monotonic() >= lease['expires']):
                    raise WorkerError('Ручная аренда истекла; обзор/очередь остаётся на паузе до явного возврата')
                return runtime, stream, lease
        raise WorkerError('Неверный token ручного владельца')

    @project_action
    def manual_acquire(self, identifier, payload):
        if set(payload) != {'role'} or payload['role'] not in ('analyzer','receiver'): raise ValueError('Выберите роль analyzer/receiver')
        role=payload['role']
        with self.lock:
            project=self.project(identifier); runtime=self.runtime.get(identifier)
            if not runtime or runtime['stop'].is_set(): raise WorkerError('Нужна работающая охрана')
            stream=project[role+'_stream_id']
            if stream in runtime['manual']: raise WorkerError('Прибор уже передан вручную; используйте возврат или снимите охрану')
            if role=='receiver': runtime['epoch']+=1
            lease=dict(token=uuid4().hex,role=role,expires=time.monotonic()+30,returning=False)
            runtime['manual'][stream]=lease
            if role=='analyzer':
                project['coverage']='paused'; self.store.put('project',project)
        if role=='analyzer': self.workers.stop_stream(runtime['sid'],stream)
        return dict(token=lease['token'],role=role,expires_seconds=30)

    @project_action
    def manual_renew(self, identifier, payload):
        if set(payload) != {'token'}: raise ValueError('Нужен только token')
        with self.lock:
            runtime,stream,lease=self._lease(identifier,payload['token'])
            lease['expires']=time.monotonic()+30
            return dict(role=lease['role'],expires_seconds=30)

    @project_action
    def manual_tune(self, identifier, payload):
        if set(payload) != {'token','frequency_hz','mode'}: raise ValueError('Нужны token, frequency_hz и mode')
        with self.lock:
            runtime,stream,lease=self._lease(identifier,payload['token'])
            if lease['role']!='receiver': raise ValueError('Перестройка относится к receiver')
            project=self.project(identifier)
            from ..serial_instruments import ARDV1SerialAdapter, _rf_command
            _rf_command(payload['frequency_hz'])
            if payload['mode'] not in ARDV1SerialAdapter.MONITOR_MODES: raise ValueError('Недоступный режим приёма')
            sid=runtime['sid']; token=lease['token']
        return self.workers.tune(sid,stream,dict(frequency_hz=payload['frequency_hz'],mode=payload['mode']),control_owner=token)

    @project_action
    def manual_return(self, identifier, payload):
        if set(payload) != {'token'}: raise ValueError('Нужен только token')
        with self.lock:
            runtime,stream,lease=self._lease(identifier,payload['token'],current=False)
            if lease['returning']: return dict(state='restoring',role=lease['role'])
            lease['returning']=True
            if lease['role']=='analyzer':
                project=self.project(identifier); project['coverage']='restoring';self.store.put('project',project)
                thread=threading.Thread(target=self._restore_analyzer,args=(identifier,runtime,stream),daemon=True)
                runtime['threads']=[t for t in runtime['threads'] if t.is_alive()]+[thread];thread.start()
                return dict(state='restoring',role='analyzer')
            runtime['epoch']+=1
            project=self.project(identifier)
            frequency=project['profile']['receiver']['settings'].get('frequency_hz',145500000)
        try:
            self.workers.tune(runtime['sid'],stream,dict(frequency_hz=frequency,mode=project.get('receiver_mode','AUTO')),control_owner=runtime['token'])
        except Exception:
            with self.lock: lease['returning']=False
            raise
        with self.lock:
            runtime['manual'].pop(stream,None)
        return dict(state='returned',role='receiver')

    @project_action
    def search_start(self, identifier, payload):
        if set(payload) != {'token','event_id','span_hz'}: raise ValueError('Нужны token, event_id и span_hz')
        with self.lock:
            runtime,stream,lease=self._lease(identifier,payload['token'])
            if lease['role']!='analyzer': raise ValueError('Поиск использует ручной захват анализатора')
            if runtime.get('search'): raise WorkerError('Сначала верните текущий узкий обзор')
            event=self.store.get('event',payload['event_id'])
            if event['project_id']!=identifier: raise KeyError('Событие другого проекта')
            span=payload['span_hz']; center=event['center_frequency_hz']
            if type(span) is not int or not 1000<=span<=100_000_000: raise ValueError('span_hz должен быть 1000..100000000')
            project=self.project(identifier); profile=copy.deepcopy(project['profile']['analyzer'])
            for field in ('id','session_id','state'):profile.pop(field,None)
            settings=profile['settings'];settings.update(start_hz=center-span//2,stop_hz=center+(span-span//2))
            if settings.get('adapter')=='tinysa-serial': validate_receive_settings(settings)
            session=self.sessions.create_session(dict(name=project['name']+' · поиск',streams=[profile],metadata={'monitoring_project_id':identifier,'source_search':True}))
            self.sessions.start_session(session.id)
            runtime['claims'].add((session.id,session.streams[0].id))
            search=dict(session_id=session.id,stream_id=session.streams[0].id,event_id=event['id'],
                        center_frequency_hz=center,span_hz=span,detector_rowid=0,maximum_level=None)
            runtime['search']=search
            project['session_ids'].append(session.id);project['coverage']='narrow';project['state']='searching'
            self.store.put('project',project)
        try:
            statuses=self.workers.start_session(session.id)
            if any(x['state']=='error' for x in statuses):raise WorkerError('Не удалось запустить узкий обзор')
        except Exception:
            with self.lock:project['coverage']='paused';self.store.put('project',project)
            raise
        return self.search_status(identifier)

    def search_status(self, identifier):
        with self.lock:
            runtime=self.runtime.get(identifier)
            if runtime:self._search_tick(identifier,runtime)
            search=copy.deepcopy(runtime.get('search')) if runtime else None
            project=self.project(identifier)
            error=runtime.get('search_error') if runtime else None
        series=[]
        if search:
            sweeps=self.sessions.list_sweeps(search['session_id'],stream_id=search['stream_id'],limit=256)
            for sweep in sweeps:
                index=max(range(len(sweep.levels)),key=sweep.levels.__getitem__)
                series.append(dict(sweep_id=sweep.id,started_at=format_time(sweep.interval.started_at),ended_at=format_time(sweep.interval.ended_at),
                                   peak_level=sweep.levels[index],frequency_hz=sweep.frequencies_hz[index]))
        with self.store.lock:
            clause=" AND json_extract(payload,'$.search_session_id')=?" if search else ''
            args=[identifier]+([search['session_id']] if search else [])
            rows=self.store.db.execute("SELECT payload FROM monitoring_records WHERE kind='position-mark' AND project_id=?"+
                clause+' ORDER BY created_at DESC,id DESC LIMIT 256',args).fetchall()
            marks=[json.loads(x[0]) for x in rows]
        return dict(search or {},active=bool(search and project['coverage']=='narrow'),coverage=project.get('coverage','stopped'),
                    series=series,maximum_level=search.get('maximum_level') if search else None,marks=marks,
                    error=error)

    def _search_tick(self, identifier, runtime):
        with self.lock:
            search=runtime.get('search')
            if not search:return
            statuses=self.workers.list_workers(search['session_id'])
            if any(not x['alive'] for x in statuses) and not runtime['manual'].get(self.project(identifier)['analyzer_stream_id'],{}).get('returning'):
                runtime['search_error']='Узкий обзор остановлен; широкое покрытие остаётся на паузе'
                project=self.project(identifier)
                if project.get('coverage')!='paused':
                    project.update(coverage='paused',last_error=runtime['search_error']);self.store.put('project',project)
            view=dict(active_session_id=search['session_id'],analyzer_stream_id=search['stream_id'],detector_rowid=search.get('detector_rowid',0),detector_cursor=search.get('detector_cursor'))
            changed=False
            for rowid,sweep in self._pending_sweeps(view):
                peak=max(sweep.levels)
                search['maximum_level']=peak if search.get('maximum_level') is None else max(peak,search['maximum_level'])
                search.update(detector_rowid=rowid,detector_cursor=sweep.id);changed=True
            if changed:
                search.setdefault('created_at',format_time(utc_now()))
                self.store.put('source-search',dict(search,id=search['session_id'],project_id=identifier))
            return changed

    @project_action
    def search_mark(self, identifier, payload):
        if set(payload) != {'token','label'}: raise ValueError('Нужны token и label')
        with self.lock:
            runtime,stream,lease=self._lease(identifier,payload['token'])
            if lease['role']!='analyzer' or not runtime.get('search'): raise WorkerError('Узкий обзор не включён')
            search=self.search_status(identifier)
            if not search['series']: raise ValueError('Ещё нет полного узкого свипа')
            latest=search['series'][-1]
            return self.store.put('position-mark',record(identifier,label=text(payload['label'],'Отметка'),
                search_session_id=search['session_id'],event_id=search['event_id'],sweep_id=latest['sweep_id'],
                level=latest['peak_level'],frequency_hz=latest['frequency_hz']))

    def _restore_analyzer(self, identifier, runtime, stream):
        try:
            lifecycle=self._project_lifecycle(identifier)
            while not lifecycle.acquire(timeout=.1):
                if runtime['stop'].is_set():return
            try:
                if runtime['stop'].is_set() or self.runtime.get(identifier) is not runtime:return
                search=runtime.get('search')
                if search:
                    self.workers.stop_session(search['session_id']);self.sessions.stop_session(search['session_id'])
                    while self._search_tick(identifier,runtime):pass
                self.workers.stop_stream(runtime['sid'],stream)
                started=utc_now()
                self.workers.start_stream(runtime['sid'],stream)
            finally:lifecycle.release()
            project=self.project(identifier);baseline=self.store.get('baseline',project['baseline_id'])
            deadline=time.monotonic()+100
            while time.monotonic()<deadline and not runtime['stop'].wait(.1):
                sweeps=self.sessions.list_sweeps(runtime['sid'],stream_id=stream,limit=1)
                if sweeps and sweeps[0].interval.started_at>=started:
                    if fingerprint(sweeps[0]) != baseline['fingerprint']:raise ValueError('После возврата профиль не совпадает с эталоном')
                    with self.lock:
                        runtime['manual'].pop(stream,None);runtime.pop('search',None);runtime.pop('search_error',None)
                        project=self.project(identifier);project.update(state='armed',coverage='wide',detector_state=None,last_error=None)
                        self.store.put('project',project)
                    return
            raise WorkerError('Широкий обзор не подтвердился после возврата')
        except Exception as exc:
            with self.lock:
                if runtime['stop'].is_set() or self.runtime.get(identifier) is not runtime:return
                runtime['error']=str(exc);project=self.project(identifier)
                project.update(state='error',coverage='error',last_error=str(exc));self.store.put('project',project)
            runtime['stop'].set()
            self.workers.stop_session(runtime['sid'])

    def event_sources(self,identifier,event_id,query):
        event=self.store.get('event',event_id)
        if event['project_id']!=identifier:raise KeyError('Событие другого проекта')
        if set(query)-{'after','limit'} or any(len(v)!=1 for v in query.values()):raise ValueError('Неверная пагинация источников')
        limit=int(query.get('limit',['64'])[0]);after=query.get('after',[''])[0]
        if not 1<=limit<=64:raise ValueError('limit должен быть 1..64')
        with self.store.lock:
            rows=self.store.db.execute("""SELECT payload FROM monitoring_records WHERE kind='event-source'
                AND project_id=? AND json_extract(payload,'$.event_id')=? AND id>? ORDER BY id LIMIT ?""",
                (identifier,event_id,after,limit+1)).fetchall()
        items=[json.loads(x[0]) for x in rows];more=len(items)>limit;items=items[:limit]
        return dict(items=items,next_cursor=items[-1]['id'] if more else None)

    def _history_sweeps(self, identifier, query):
        project=self.project(identifier)
        allowed={'limit','after','start','end','min_hz','max_hz','event_id'}
        if set(query)-allowed or any(len(x)!=1 for x in query.values()):raise ValueError('Неверные фильтры истории')
        limit=int(query.get('limit',['32'])[0])
        if not 1<=limit<=64:raise ValueError('limit должен быть 1..64')
        minimum=int(query.get('min_hz',['0'])[0]);maximum=int(query.get('max_hz',['20000000000'])[0])
        if not 0<=minimum<=maximum<=20000000000:raise ValueError('Неверный диапазон истории')
        start=_archive_time(parse_time(query['start'][0])) if query.get('start') else '0000'
        end=_archive_time(parse_time(query['end'][0])) if query.get('end') else '9999'
        if start>end:raise ValueError('Начало позже окончания')
        cursor=query.get('after',[''])[0];previous=('9999','~')
        if cursor:
            try:
                previous=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if len(previous)!=2 or not all(isinstance(x,str) for x in previous):raise ValueError()
                parse_time(previous[0])
            except Exception as exc:raise ValueError('Неверный курсор истории') from exc
        refs=None
        event_id=query.get('event_id',[''])[0]
        if event_id:
            event=self.store.get('event',event_id)
            if event['project_id']!=identifier:raise KeyError('Событие другого проекта')
        store=self.sessions._store
        if store:
            args=[identifier,start,end,*previous,minimum,maximum]
            clause=''
            if event_id:
                clause=" AND EXISTS (SELECT 1 FROM monitoring_records e WHERE e.kind='event-source' AND e.project_id=? AND json_extract(e.payload,'$.event_id')=? AND json_extract(e.payload,'$.sweep_id')=s.id)"
                args.extend([identifier,event_id])
            args.append(limit+1)
            with store._lock:
                rows=store._connection.execute('''SELECT s.* FROM monitoring_sweep_index i
                    JOIN sweeps s ON s.id=i.sweep_id JOIN monitoring_sessions p ON p.session_id=i.session_id
                    WHERE p.project_id=? AND i.started_at>=? AND i.started_at<=? AND (i.started_at,s.id)<(?,?)
                    AND i.max_hz>=? AND i.min_hz<=?'''+clause+''' ORDER BY i.started_at DESC,s.id DESC LIMIT ?''',args).fetchall()
                sweeps=[store._sweep_from_row(x) for x in rows]
        else:
            with self.sessions._lock:sweeps=copy.deepcopy([s for s in self.sessions._sweeps if s.session_id in project['session_ids']])
            if event_id:
                refs={x['sweep_id'] for x in self.store.list('event-source',identifier,limit=1000) if x['event_id']==event_id}
            sweeps=[s for s in sweeps if start<=_archive_time(s.interval.started_at)<=end and
                    (_archive_time(s.interval.started_at),s.id)<tuple(previous) and s.frequencies_hz[-1]>=minimum and s.frequencies_hz[0]<=maximum and (refs is None or s.id in refs)]
            sweeps.sort(key=lambda s:(s.interval.started_at,s.id),reverse=True);sweeps=sweeps[:limit+1]
        more=len(sweeps)>limit;sweeps=sweeps[:limit]
        after=base64.urlsafe_b64encode(json.dumps([_archive_time(sweeps[-1].interval.started_at),sweeps[-1].id]).encode()).decode() if sweeps and more else None
        return sweeps,after,minimum,maximum

    def history(self, identifier, query):
        sweeps,cursor,minimum,maximum=self._history_sweeps(identifier,query)
        items=[]
        for sweep in sweeps:
            indexes=[i for i,hz in enumerate(sweep.frequencies_hz) if minimum<=hz<=maximum]
            trace=Trace([sweep.frequencies_hz[i] for i in indexes],[sweep.levels[i] for i in indexes]).decimate(320)
            value=to_primitive(sweep);value.update(frequencies_hz=[int(hz) for hz in trace.frequencies],levels=trace.levels.tolist(),
                original_point_count=len(sweep.levels),display_only=True)
            value['settings']={k:v for k,v in sweep.settings.items() if k in (
                'adapter','rbw_hz','rbw_requested_hz','receive_profile','generator_output','overload','incomplete','simulated')
                and (v is None or type(v) in (int,float,bool) or isinstance(v,str) and len(v)<=200)}
            value['settings_available_in_original']=True
            items.append(value)
        return dict(items=items,next_cursor=cursor)

    def history_original(self, identifier, sweep_id):
        project=self.project(identifier);store=self.sessions._store
        if store:
            sweep=store.get_sweep(sweep_id)
        else:
            with self.sessions._lock:sweep=next((s for s in self.sessions._sweeps if s.id==sweep_id),None)
        if sweep is None or sweep.session_id not in project['session_ids']:raise KeyError('Свип другого проекта/отсутствует')
        return to_primitive(sweep)

    def history_comparison(self, identifier, sweep_id):
        from ..contracts import Sweep
        project=self.project(identifier);baseline=self.store.get('baseline',project['baseline_id'])
        sweep=Sweep.from_dict(self.history_original(identifier,sweep_id))
        compatible=fingerprint(sweep)==baseline['fingerprint']
        items=[]
        if compatible:
            selected=Trace(sweep.frequencies_hz,sweep.levels).decimate(320)
            indexes={hz:i for i,hz in enumerate(sweep.frequencies_hz)}
            for hz,level in zip(selected.frequencies,selected.levels):
                i=indexes[int(hz)];items.append(dict(frequency_hz=int(hz),baseline_p50_dbm=baseline['p50_dbm'][i],
                    baseline_p95_dbm=baseline['p95_dbm'][i],current_dbm=float(level),delta_db=float(level)-baseline['p50_dbm'][i]))
        return dict(baseline_id=baseline['id'],sweep_id=sweep_id,detector_version=baseline['detector_version'],compatible=compatible,
                    reason=None if compatible else 'Сетка/настройки не совпадают с эталоном',items=items,display_only=True)
