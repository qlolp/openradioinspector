"""Durable references, allowlists, detector cursors and evidence events."""
import copy
from datetime import datetime, timezone

from ..contracts import format_time, parse_time, to_primitive
from .detector import Detector, build_baseline, VERSION
from .models import record, text


class BaselineOperations:
    def create_baseline(self, identifier, payload):
        if set(payload) - {'name', 'location', 'confirm'} or payload.get('confirm') is not True:
            raise ValueError('Подтвердите пригодность эталона обычной обстановки')
        with self.lock:
            project = self.project(identifier)
            if identifier in self.runtime or project['state'] in ('armed', 'searching','degraded'):
                raise ValueError('Снимите проект с охраны перед заменой эталона')
            sweeps,period = self.baseline_samples(project)
            baseline = record(identifier, name=text(payload.get('name'), 'Название эталона'),
                              location=text(payload.get('location'), 'Место'), **build_baseline(sweeps))
            baseline['confirmed'] = True
            baseline['sweep_period_seconds']=period
            baseline['sample_strategy']='uniform_window_plus_latest_twenty'
            baseline['preparation']=copy.deepcopy(project.get('preparation',{}))
            project.update(baseline_id=baseline['id'], state='ready', detector_state=None,
                           detector_cursor=sweeps[-1].id)
            store = self.sessions._store
            if store is not None:
                with store._lock:
                    project['detector_rowid'] = store._connection.execute(
                        'SELECT MAX(rowid) FROM sweeps WHERE id IN (' + ','.join('?' for _ in sweeps) + ')',
                        [s.id for s in sweeps]).fetchone()[0] or 0
            with self.store.transaction():
                self.store.put('baseline',baseline)
                self.store.put('project', project)
            return baseline

    def add_allowed(self, identifier, payload):
        self.project(identifier)
        if set(payload) - {'name','start_hz','end_hz','comment','watch_changes'}:
            raise ValueError('Неизвестное поле разрешённого сигнала')
        if len(self.store.list('allowed', identifier, limit=1000)) >= 1000:
            raise ValueError('Предел каталога: 1000 правил')
        start, end = payload.get('start_hz'), payload.get('end_hz')
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= 20_000_000_000:
            raise ValueError('Требуется допустимый диапазон в целых Гц')
        watch = payload.get('watch_changes', False)
        if type(watch) is not bool: raise ValueError('watch_changes должен быть boolean')
        return self.store.put('allowed', record(identifier, name=text(payload.get('name'), 'Название'),
            start_hz=start, end_hz=end, comment=str(payload.get('comment',''))[:1000], watch_changes=watch))

    def _pending_sweeps(self, project, limit=64):
        sid, stream = project['active_session_id'], project['analyzer_stream_id']
        cursor = project.get('detector_cursor')
        store = self.sessions._store
        if store is not None:
            with store._lock:
                rows = store._connection.execute('SELECT rowid AS capture_order, * FROM sweeps WHERE session_id=? AND stream_id=? AND rowid>? ORDER BY rowid LIMIT ?',
                    (sid, stream, project.get('detector_rowid', 0), limit)).fetchall()
                return [(row['capture_order'], store._sweep_from_row(row)) for row in rows]
        with self.sessions._lock:
            items = [x for x in self.sessions._sweeps if x.session_id == sid and x.stream_id == stream]
            index = next((i+1 for i,x in enumerate(items) if x.id == cursor), 0)
            return [(i+1, copy.deepcopy(s)) for i,s in enumerate(items) if index <= i < index+limit]

    def analyze(self, identifier):
        with self.lock:
            project = self.project(identifier)
            if not project.get('baseline_id'): raise ValueError('Нужен подтверждённый эталон')
            baseline = self.store.get('baseline', project['baseline_id'])
            detector = Detector(baseline, project['parameters'], project.get('detector_state'))
            allowed = self.store.list('allowed', identifier, limit=1000)
            events = set()
            processed = 0
            quality_issues=0
            for rowid, sweep in self._pending_sweeps(project):
                regions, excess, thresholds = detector.step(sweep, allowed)
                opens = self.store.open_events(identifier,sweep.session_id,baseline['id'])
                with self.store.transaction():
                    quality=detector.state.pop('quality_issue',None)
                    if quality:
                        self.store.put('quality',record(identifier,sweep_id=sweep.id,session_id=sweep.session_id,
                            stream_id=sweep.stream_id,**quality))
                        quality_issues+=1
                        for interrupted in opens:
                            interrupted.update(state='interrupted',end_reason='data_gap')
                            self.store.put('event',interrupted)
                        opens=[]
                        if project['state'] in ('armed','searching','degraded'):
                            self._notify(project,sweep.id,'data_gap','warning','Разрыв измерений; подтверждение 3/5 начато заново',None)
                    touched = set()
                    for region in regions:
                        event = next((x for x in opens if x['start_frequency_hz'] <= region['end_frequency_hz']
                            and x['end_frequency_hz'] >= region['start_frequency_hz']
                            and (x['kind'] in ('broadband_change','dense_change')) == (region['kind'] in ('broadband_change','dense_change'))), None)
                        if event is None:
                            evidence = [x for x,row in zip(detector.state['recent_sources'], detector.state['window'])
                                        if any(row[region['first_bin']:region['last_bin']+1])]
                            event = record(identifier, state='open', disposition='unreviewed',
                                run_session_id=sweep.session_id,
                                first_seen=evidence[0]['started_at'] if evidence else format_time(sweep.interval.started_at), source_count=0,
                                source_refs=[], baseline_id=baseline['id'], detector_version=VERSION, **region)
                            event['detector_parameters']=copy.deepcopy(project['parameters'])
                        if region['peak_excess_db'] > event.get('peak_excess_db', -1): event.update(region)
                        event.update(last_seen=format_time(sweep.interval.ended_at), quiet_sweeps=0)
                        source = dict(session_id=sweep.session_id, stream_id=sweep.stream_id, sweep_id=sweep.id)
                        if not event['source_count']:
                            for prior in evidence[:-1]:
                                ref = {k:v for k,v in prior.items() if k != 'started_at'}
                                self.store.put('event-source', dict(id=event['id']+'.'+prior['sweep_id'], project_id=identifier,
                                    created_at=event['created_at'], event_id=event['id'], **ref))
                                event['source_refs'].append(ref); event['source_count'] += 1
                        event['source_count'] += 1
                        event['source_refs'] = (event['source_refs'] + [source])[-100:]
                        self.store.put('event-source', dict(id=event['id']+'.'+sweep.id, project_id=identifier,
                            created_at=event['created_at'], event_id=event['id'], **source))
                        self.store.put('event', event); touched.add(event['id']); events.add(event['id'])
                        if project['state'] in ('armed','searching','degraded'):
                            self.notify_event(project,event)
                    for event in opens:
                        if event['id'] in touched: continue
                        lo, hi = event['first_bin'], event['last_bin']
                        if all(excess[i] < thresholds[i]-project['parameters']['hysteresis_db'] for i in range(lo,hi+1)):
                            event['quiet_sweeps'] = event.get('quiet_sweeps',0)+1
                            if event['quiet_sweeps'] >= 5: event['state']='closed'
                            self.store.put('event', event)
                        else:
                            event['quiet_sweeps'] = 0; self.store.put('event', event)
                    project.update(detector_cursor=sweep.id, detector_rowid=rowid, detector_state=detector.state)
                    self.store.put('project', project)
                processed += 1
            return dict(project_id=identifier, event_ids=sorted(events)[:100], events_updated=len(events),
                        processed_sweeps=processed, quality_issues=quality_issues,cursor=project.get('detector_cursor'))

    def review_event(self, identifier, event_id, payload):
        with self.lock:
            self.project(identifier)
            event = self.store.get('event', event_id)
            if event['project_id'] != identifier: raise KeyError('Событие другого проекта')
            disposition = payload.get('disposition')
            if set(payload)-{'disposition','comment'} or disposition not in ('unreviewed','authorized','false_positive','investigate'):
                raise ValueError('Неизвестный операторский вердикт')
            event.update(disposition=disposition, operator_comment=str(payload.get('comment',''))[:1000])
            return self.store.put('event', event)
