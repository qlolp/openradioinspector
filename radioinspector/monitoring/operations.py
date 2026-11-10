"""Durable alarms/delivery, explicit restart intent and archive budgets."""
import base64
from collections import deque
from datetime import timedelta
import hashlib
import hmac
import json
import math
from pathlib import Path
import shutil
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from ..contracts import format_time, parse_time, to_primitive, utc_now
from ..device_workers import WorkerError
from ..session_store import _archive_time
from .models import text


DEFAULT_ARCHIVE = dict(minimum_free_bytes=256*1024*1024,minimum_retention_seconds=3600)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):return None


class OperationalMonitoring:
    def initialize_operations(self):
        self._supervisor_stop=threading.Event();self._supervisor=None
        self._capacity_samples=deque(maxlen=60)
        self._delivery_opener=urllib.request.build_opener(NoRedirect())
        self._delivery_error=None

    def settings(self, identifier):
        project=self.project(identifier)
        with self.store.lock:
            hook=self.store.db.execute('SELECT url,secret FROM monitoring_webhooks WHERE project_id=?',(identifier,)).fetchone()
        return dict(autostart=project.get('autostart',False),archive=project.get('archive',DEFAULT_ARCHIVE),
                    detector=project['parameters'],webhook=dict(enabled=project.get('webhook_enabled',False),
                    url=hook['url'] if hook else '',secret_configured=bool(hook and hook['secret'])))

    def update_settings(self, identifier, payload):
        if set(payload)-{'autostart','archive','webhook','detector'}:raise ValueError('Неизвестная настройка проекта')
        with self.lock:
            project=self.project(identifier);settings=self.settings(identifier)
            if 'autostart' in payload:
                if type(payload['autostart']) is not bool:raise ValueError('autostart должен быть boolean')
                project['autostart']=payload['autostart']
            if 'archive' in payload:
                value=payload['archive']
                if not isinstance(value,dict) or set(value)-set(DEFAULT_ARCHIVE):raise ValueError('Неверная настройка архива')
                value=dict(settings['archive'],**value)
                if type(value['minimum_free_bytes']) is not int or not 1024*1024<=value['minimum_free_bytes']<=10**15:raise ValueError('Резерв должен быть не меньше 1 МиБ')
                if type(value['minimum_retention_seconds']) is not int or not 60<=value['minimum_retention_seconds']<=31536000:raise ValueError('Нужен прогноз хранения 60..31536000 секунд')
                project['archive']=value
            if 'detector' in payload:
                if identifier in self.runtime:raise ValueError('Снимите охрану перед изменением детектора')
                value=payload['detector'];limits=dict(margin_db=(1,60),impulse_db=(1,120),hysteresis_db=(0,20),dwell_seconds=(1,30))
                if not isinstance(value,dict) or set(value)-set(project['parameters']):raise ValueError('Неизвестный параметр детектора')
                parameters=dict(project['parameters'],**value)
                for key,(low,high) in limits.items():
                    x=parameters[key]
                    if type(x) not in (int,float) or not math.isfinite(x) or not low<=x<=high:raise ValueError('Недопустимый параметр '+key)
                if type(parameters['window_sweeps']) is not int or type(parameters['confirmations']) is not int or not 1<=parameters['confirmations']<=parameters['window_sweeps']<=15:raise ValueError('Неверное окно подтверждения')
                if parameters['impulse_db']<parameters['margin_db'] or parameters['hysteresis_db']>parameters['margin_db']:raise ValueError('Импульс/гистерезис не согласованы с порогом')
                project['parameters']=parameters;project['detector_state']=None
            hook=None
            if 'webhook' in payload:
                value=payload['webhook']
                if not isinstance(value,dict) or set(value)-{'enabled','url','secret'}:raise ValueError('Неверная настройка webhook')
                enabled=value.get('enabled',settings['webhook']['enabled'])
                if type(enabled) is not bool:raise ValueError('enabled должен быть boolean')
                url=value.get('url',settings['webhook']['url'])
                if not isinstance(url,str) or len(url)>2048:raise ValueError('Неверный URL webhook')
                parsed=urlsplit(url)
                if url and (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment):raise ValueError('Требуется HTTP(S) URL без учётных данных и fragment')
                with self.store.lock:
                    old=self.store.db.execute('SELECT secret FROM monitoring_webhooks WHERE project_id=?',(identifier,)).fetchone()
                secret=value.get('secret',old[0] if old else '')
                if not isinstance(secret,str) or len(secret)>1024:raise ValueError('Неверный секрет webhook')
                if enabled and (not url or not secret):raise ValueError('Для webhook нужны адрес и секрет')
                hook=(identifier,url,secret);project['webhook_enabled']=enabled
            with self.store.transaction():
                if hook:self.store.db.execute('INSERT INTO monitoring_webhooks VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET url=excluded.url,secret=excluded.secret',hook)
                self.store.put('project',project)
            return self.settings(identifier)

    def notify_event(self, project, event):
        return self._notify(project,event['id'],event['kind'],'warning',
            'Обнаружено излучение / отклонение: '+str(event['center_frequency_hz'])+' Гц',event['id'])

    def notify_error(self, project, message, kind='coverage_error'):
        return self._notify(project,project['active_session_id'],kind,'critical',message,None)

    def volatile_error(self,project,message):
        identifier=uuid5(NAMESPACE_URL,project['id']+':'+project['active_session_id']+':coverage_error').hex
        return dict(id=identifier,notification_id=identifier,project_id=project['id'],event_id=None,
            created_at=_archive_time(utc_now()),severity='critical',kind='coverage_error',message=message,
            acknowledged_at=None,volatile=True,delivery=dict(state='unavailable',attempts=0,
                next_attempt_at=None,last_error='archive_unavailable'))

    def _notify(self, project, source, kind, severity, message, event_id):
        identifier=uuid5(NAMESPACE_URL,project['id']+':'+source+':'+kind).hex
        now=_archive_time(utc_now())
        payload=dict(notification_id=identifier,project_id=project['id'],event_id=event_id,
                     kind=kind,severity=severity,message=message,occurred_at=now)
        with self.store.transaction():
            self.store.db.execute('''INSERT OR IGNORE INTO monitoring_notifications
                (id,project_id,created_at,payload,state,attempts,next_at,error,lease_until,acknowledged_at)
                VALUES (?,?,?,?,?,0,?,NULL,NULL,NULL)''',
                (identifier,project['id'],now,json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')),
                 'pending' if project.get('webhook_enabled') else 'disabled',now))
        return identifier

    def alerts(self, identifier, query):
        self.project(identifier)
        if set(query)-{'limit','after'} or any(len(v)!=1 for v in query.values()):raise ValueError('Неверная пагинация тревог')
        limit=int(query.get('limit',['50'])[0]);after=query.get('after',[''])[0];cursor=('9999','~')
        if not 1<=limit<=100:raise ValueError('limit должен быть 1..100')
        if after:
            try:
                cursor=json.loads(base64.urlsafe_b64decode(after).decode())
                if len(cursor)!=2 or not all(isinstance(x,str) for x in cursor):raise ValueError()
                parse_time(cursor[0])
            except Exception as exc:raise ValueError('Неверный курсор тревог') from exc
        with self.store.lock:
            rows=self.store.db.execute('''SELECT * FROM monitoring_notifications WHERE project_id=?
                AND (created_at,id)<(?,?) ORDER BY created_at DESC,id DESC LIMIT ?''',
                (identifier,*cursor,limit+1)).fetchall()
        items=[]
        for row in rows:
            data=json.loads(row['payload']);data.update(id=row['id'],created_at=row['created_at'],acknowledged_at=row['acknowledged_at'],
                delivery=dict(state=row['state'],attempts=row['attempts'],next_attempt_at=row['next_at'],last_error=row['error']))
            items.append(data)
        with self.lock:
            volatile=self.runtime.get(identifier,{}).get('critical_alert')
            if volatile and volatile.get('volatile') and not after and not any(x['id']==volatile['id'] for x in items):
                items.append(dict(volatile))
        items.sort(key=lambda x:(x['created_at'],x['id']),reverse=True)
        more=len(items)>limit;items=items[:limit]
        next_cursor=base64.urlsafe_b64encode(json.dumps([items[-1]['created_at'],items[-1]['id']]).encode()).decode() if more else None
        return dict(items=items,next_cursor=next_cursor)

    def acknowledge_alert(self, identifier, alert_id, payload):
        if payload:raise ValueError('Подтверждение принимает пустой объект')
        self.project(identifier)
        with self.lock:
            volatile=self.runtime.get(identifier,{}).get('critical_alert')
            if volatile and volatile['id']==alert_id:
                volatile['acknowledged_at']=_archive_time(utc_now())
                if volatile.get('volatile'):return dict(id=alert_id,acknowledged=True,volatile=True)
        with self.store.transaction():
            row=self.store.db.execute('SELECT id FROM monitoring_notifications WHERE id=? AND project_id=?',(alert_id,identifier)).fetchone()
            if not row:raise KeyError('Тревога другого проекта/отсутствует')
            self.store.db.execute('UPDATE monitoring_notifications SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?',
                                  (_archive_time(utc_now()),alert_id))
        return dict(id=alert_id,acknowledged=True)

    def deliver_one(self, now=None):
        now=now or utc_now();stamp=_archive_time(now)
        with self.store.transaction():
            self.store.db.execute("UPDATE monitoring_notifications SET state='failed',error='last_attempt_interrupted',lease_until=NULL WHERE state='delivering' AND attempts>=5 AND lease_until<=?",(stamp,))
            row=self.store.db.execute('''SELECT * FROM monitoring_notifications WHERE attempts<5 AND
                ((state='pending' AND next_at<=?) OR (state='delivering' AND lease_until<=?))
                ORDER BY created_at,id LIMIT 1''',(stamp,stamp)).fetchone()
            if not row:return False
            project=self.project(row['project_id'])
            config=self.store.db.execute('SELECT url,secret FROM monitoring_webhooks WHERE project_id=?',(project['id'],)).fetchone()
            if not project.get('webhook_enabled') or not config:
                self.store.db.execute("UPDATE monitoring_notifications SET state='disabled' WHERE id=?",(row['id'],));return True
            attempts=row['attempts']+1
            claim_lease=_archive_time(now+timedelta(seconds=30))
            self.store.db.execute("UPDATE monitoring_notifications SET state='delivering',attempts=?,lease_until=? WHERE id=?",
                (attempts,claim_lease,row['id']))
        raw=row['payload'].encode('utf-8');signature=hmac.new(config['secret'].encode(),raw,hashlib.sha256).hexdigest()
        request=urllib.request.Request(config['url'],data=raw,method='POST',headers={
            'Content-Type':'application/json','X-OpenRadioInspector-Notification-ID':row['id'],
            'X-OpenRadioInspector-Signature':'sha256='+signature})
        error=None
        try:
            with self._delivery_opener.open(request,timeout=3) as response:
                if not 200<=response.status<300:error='HTTP '+str(response.status)
        except urllib.error.HTTPError as exc:
            error='HTTP '+str(exc.code);exc.close()
        except Exception:error='network_error'
        with self.store.transaction():
            self.store.db.execute('''UPDATE monitoring_notifications SET state=?,next_at=?,error=?,lease_until=NULL
                WHERE id=? AND attempts=? AND state='delivering' AND lease_until=?''',
                ('sent' if error is None else ('failed' if attempts>=5 else 'pending'),
                 None if error is None or attempts>=5 else _archive_time(now+timedelta(seconds=min(300,5*2**attempts))),error,row['id'],attempts,claim_lease))
        return True

    def storage_report(self, identifier, *, probe=False):
        project=self.project(identifier);config=project.get('archive',DEFAULT_ARCHIVE);capacity=self.sessions.archive_capacity()
        store=self.sessions._store;free=None;rate=None;writable=True
        if store and store.persistent:
            free=shutil.disk_usage(Path(store.path).expanduser().resolve().parent).free
            if probe:
                with store._lock:
                    if not store._connection.in_transaction:
                        try:
                            store._connection.execute('BEGIN IMMEDIATE')
                            store._connection.execute('UPDATE monitoring_schema SET version=version')
                        except Exception:writable=False
                        finally:store._connection.rollback()
            sweeps=self.sessions.list_sweeps(project['active_session_id'],stream_id=project['analyzer_stream_id'],limit=128)
            if len(sweeps)>=2:
                elapsed=(sweeps[-1].interval.ended_at-sweeps[0].interval.started_at).total_seconds()
                if elapsed>0:rate=2*sum(len(json.dumps(to_primitive(s),ensure_ascii=False).encode()) for s in sweeps)/elapsed
            now=time.monotonic();size=capacity['database_bytes']
            if not self._capacity_samples or now-self._capacity_samples[-1][0]>=1:self._capacity_samples.append((now,size))
            if len(self._capacity_samples)>=2:
                elapsed=now-self._capacity_samples[0][0]
                if elapsed>=5:rate=max(rate or 0,max(0,size-self._capacity_samples[0][1])/elapsed)
        remaining=capacity.get('remaining_bytes')
        available=max(0,free-config['minimum_free_bytes']) if free is not None else 0
        if remaining is not None:available=min(available,remaining)
        retention=available/rate if rate and rate>0 else None
        ready=bool(store and store.persistent and writable and available>0 and capacity.get('state')!='full'
                   and (retention is None or retention>=config['minimum_retention_seconds']))
        return dict(capacity=capacity,filesystem_free_bytes=free,minimum_free_bytes=config['minimum_free_bytes'],
                    available_bytes=available,bytes_per_second=rate,estimated_retention_seconds=retention,
                    required_retention_seconds=config['minimum_retention_seconds'],ready=ready,write_probe=writable if probe else None,
                    rate_source='measured_archive_or_conservative_payload_window',automatic_deletion=False)

    def start(self):
        if self._supervisor or self._closed:return
        self._supervisor=threading.Thread(target=self._supervise,name='monitoring-supervisor',daemon=True);self._supervisor.start()

    def _recover_projects(self):
        after=''
        while not self._supervisor_stop.is_set():
            items=self.store.list('project',limit=100,after=after)
            if not items:break
            for value in items:
                identifier=value['id']
                with self._project_lifecycle(identifier):
                    with self.lock:
                        if self._closed or self._supervisor_stop.is_set():return
                        if identifier in self.runtime:continue
                        project=self.project(identifier)
                        requested=project.get('resume_requested') or project['state'] in ('armed','searching')
                        if not requested:continue
                        with self.store.transaction():
                            rows=self.store.db.execute("SELECT id,payload FROM monitoring_records WHERE kind='event' AND project_id=? AND json_extract(payload,'$.analysis.state') IN ('queued','listening','decoding')",(identifier,)).fetchall()
                            for row in rows:
                                event=json.loads(row['payload']);event['analysis'].update(state='interrupted',reason='Ядро перезапущено; старое задание не повторяется')
                                self.store.put('event',event)
                            project.update(state='interrupted',coverage='stopped',resume_requested=False);self.store.put('project',project)
                    if project.get('autostart'):
                        try:self.arm(identifier,{})
                        except Exception:
                            with self.lock:
                                project=self.project(identifier);project.update(state='error',last_error='Восстановление охраны не прошло проверку приборов/эталона/архива')
                                self.store.put('project',project);self.notify_error(project,project['last_error'],'restart_error')
            after=items[-1]['id']

    def _supervise(self):
        try:self._recover_projects()
        except Exception:self._delivery_error='recovery_archive_error'
        while not self._supervisor_stop.wait(.5):
            try:self.deliver_one()
            except Exception:self._delivery_error='notification_archive_error'
