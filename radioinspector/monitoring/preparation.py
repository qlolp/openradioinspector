"""Operator declarations and bounded collection sampling; receive-only startup."""
import copy
import statistics
from datetime import timedelta

from ..contracts import format_time, utc_now
from ..device_workers import WorkerError
from ..session_store import _archive_time
from .models import text
from .research import project_action


class PreparationOperations:
    @project_action
    def save_preparation(self,identifier,payload):
        if set(payload)!={'antenna_notes','receive_path_confirmed','location_confirmed'}:
            raise ValueError('Нужны антенна, подтверждения тракта и места')
        notes=text(payload['antenna_notes'],'Антенна/тракт',1000)
        if any(type(payload[k]) is not bool for k in ('receive_path_confirmed','location_confirmed')):
            raise ValueError('Подтверждения должны быть boolean')
        with self.lock:
            if identifier in self.runtime or identifier in self.preparing:raise WorkerError('Остановите охрану/сбор перед изменением подготовки')
            project=self.project(identifier);project['preparation']=dict(payload,antenna_notes=notes,confirmed_at=format_time(utc_now()))
            return self.store.put('project',project)

    @project_action
    def start_preparation(self,identifier,payload):
        if payload:raise ValueError('Запуск сбора принимает пустой объект')
        with self.lock:
            if self._closed:raise WorkerError('Ядро закрывается')
            if identifier in self.runtime:raise WorkerError('Сначала снимите охрану')
            project=self.project(identifier)
            if identifier in self.preparing:
                return dict(project=project,workers=self.workers.list_workers(project['active_session_id']))
            # A replacement collection must not depend on the old baseline's
            # availability or compatibility; guard startup still checks both.
            if not self.preflight(identifier,for_preparation=True)['ready']:raise ValueError('Подготовка не завершена')
            old_sid=project['active_session_id']
            if old_sid in self.preparing.values():raise WorkerError('Сеанс уже используется для другого сбора')
            if any(r['sid']==old_sid for r in self.runtime.values()):raise WorkerError('Сеанс занят другим проектом')
            self.workers.stop_session(old_sid)
            if self.sessions.get_session(old_sid).state.value=='running':self.sessions.stop_session(old_sid)
            profiles=[]
            for role in ('analyzer','receiver'):
                value=copy.deepcopy(project['profile'][role])
                for key in ('id','session_id','state'):value.pop(key,None)
                profiles.append(value)
            session=self.sessions.create_session(dict(name=project['name']+' · эталон',streams=profiles,
                metadata={'monitoring_project_id':identifier,'baseline_collection':True}))
            self.sessions.start_session(session.id)
            project.update(active_session_id=session.id,analyzer_stream_id=session.streams[0].id,
                receiver_stream_id=session.streams[1].id,session_ids=(project['session_ids']+[session.id])[-128:],
                state='collecting',coverage='preparation')
            self.preparing[identifier]=session.id
            self.store.put('project',project)
        statuses=self.workers.start_session(session.id)
        if any(x['state']=='error' for x in statuses):
            self.stop_preparation(identifier,{})
            raise WorkerError('Не удалось начать приём; проверьте исполнителей')
        return dict(project=project,workers=statuses)

    @project_action
    def stop_preparation(self,identifier,payload):
        if payload:raise ValueError('Остановка сбора принимает пустой объект')
        with self.lock:
            project=self.project(identifier);sid=self.preparing.get(identifier)
        if sid:
            self.workers.stop_session(sid)
            if self.sessions.get_session(sid).state.value=='running':self.sessions.stop_session(sid)
        with self.lock:
            self.preparing.pop(identifier,None)
            if identifier not in self.runtime:
                project=self.project(identifier);project.update(state='ready' if project['baseline_id'] else 'draft',coverage='stopped')
                self.store.put('project',project)
        return dict(project=project,workers=self.workers.list_workers(project['active_session_id']))

    def baseline_collection(self,project):
        store=self.sessions._store;sid=project['active_session_id'];stream=project['analyzer_stream_id']
        if store:
            with store._lock:
                row=store._connection.execute('''SELECT COUNT(*) AS count,MIN(i.started_at) AS first,MAX(i.ended_at) AS last
                    FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id WHERE i.session_id=? AND s.stream_id=?''',(sid,stream)).fetchone()
            from ..contracts import parse_time
            elapsed=(parse_time(row['last'])-parse_time(row['first'])).total_seconds() if row['count'] else 0.
            count=row['count']
        else:
            sweeps=self.sessions.list_sweeps(sid,stream_id=stream,limit=None)
            count=len(sweeps);elapsed=(sweeps[-1].interval.ended_at-sweeps[0].interval.started_at).total_seconds() if sweeps else 0.
        return dict(sweeps=count,elapsed_seconds=elapsed,minimum_sweeps=20,minimum_seconds=60,ready=count>=20 and elapsed>=60)

    def baseline_samples(self,project):
        sid=project['active_session_id'];stream=project['analyzer_stream_id'];store=self.sessions._store
        recent=self.sessions.list_sweeps(sid,stream_id=stream,limit=20)
        if len(recent)<20:return recent,None
        periods=[(b.interval.started_at-a.interval.started_at).total_seconds() for a,b in zip(recent,recent[1:])]
        period=statistics.median([x for x in periods if x>0]) if any(x>0 for x in periods) else 1.
        target=recent[-1].interval.ended_at-timedelta(seconds=60)
        if not store:
            all_items=self.sessions.list_sweeps(sid,stream_id=stream,limit=None)
            older=[s for s in all_items if s.interval.started_at<=target]
            start=min(recent[0].interval.started_at,older[-1].interval.started_at) if older else recent[0].interval.started_at
            selected=[s for s in all_items if s.interval.started_at>=start]
            stride=max(1,len(selected)//236)
            values={s.id:s for s in selected[::stride][:236]+recent}
            return sorted(values.values(),key=lambda s:(s.interval.started_at,s.id)),period
        with store._lock:
            row=store._connection.execute('''SELECT s.* FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id
                WHERE i.session_id=? AND s.stream_id=? AND i.started_at<=? ORDER BY i.started_at DESC,s.id DESC LIMIT 1''',
                (sid,stream,_archive_time(target))).fetchone()
            start=min(recent[0].interval.started_at,store._sweep_from_row(row).interval.started_at) if row else recent[0].interval.started_at
            end=recent[-1].interval.started_at;values={s.id:s for s in recent}
            for index in range(236):
                stamp=_archive_time(start+(end-start)*(index/235))
                row=store._connection.execute('''SELECT s.* FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id
                    WHERE i.session_id=? AND s.stream_id=? AND i.started_at>=? AND i.started_at<=?
                    ORDER BY i.started_at,s.id LIMIT 1''',(sid,stream,stamp,_archive_time(end))).fetchone()
                if row:
                    sweep=store._sweep_from_row(row);values[sweep.id]=sweep
        return sorted(values.values(),key=lambda s:(s.interval.started_at,s.id)),period
