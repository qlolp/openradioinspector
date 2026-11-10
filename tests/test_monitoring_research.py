import unittest
import threading
import time
import http.client
import json
from datetime import timedelta
from unittest.mock import patch

from tests import test_monitoring_guard as guard_tests
from tests.test_monitoring_detector import sweep
from tests.test_device_workers import simulated_session_payload, wait_until
from radioinspector.device_workers import WorkerError, TinySASimulator
from radioinspector.contracts import TimeInterval, utc_now


class ResearchTests(unittest.TestCase):
    setUp = guard_tests.GuardTests.setUp
    def prepare(self):
        session = self.sessions.create_session(simulated_session_payload(tinysa_settings={'noise_sigma_db':0,'emitters':[]}))
        project = self.service.create(dict(name='Research',site='Fixture',session_id=session.id))
        adapter = TinySASimulator(session.streams[0]); now = utc_now()
        for i in range(20):
            item=adapter.poll(); start=now-timedelta(seconds=80-4*i)
            item.interval=TimeInterval(start,start+timedelta(seconds=1)); self.sessions.ingest_sweep(item)
        self.service.create_baseline(project['id'],dict(name='Quiet',location='Fixture',confirm=True))
        project=self.service.arm(project['id'],{}); wait_until(lambda:self.workers.active_count==2,'workers')
        return project

    def test_history_is_paged_scoped_and_preserves_original(self):
        for i in range(20): self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        first=self.service.history(self.project['id'],{'limit':['3']})
        self.assertEqual(len(first['items']),3)
        second=self.service.history(self.project['id'],{'limit':['3'],'after':[first['next_cursor']]})
        self.assertFalse({x['id'] for x in first['items']} & {x['id'] for x in second['items']})
        raw=self.service.history_original(self.project['id'],first['items'][0]['id'])
        self.assertEqual(len(raw['levels']),9); self.assertTrue(first['items'][0]['display_only'])
        foreign=self.sessions.create_session(simulated_session_payload())
        self.sessions.ingest_sweep(sweep(40,session=foreign.id,stream=foreign.streams[0].id))
        with self.assertRaises(KeyError):self.service.history_original(self.project['id'],'sweep-40')

    def test_manual_receiver_pauses_queue_requires_token_and_returns_profile(self):
        project=self.prepare(); lease=self.service.manual_acquire(project['id'],{'role':'receiver'})
        with self.assertRaises(WorkerError): self.workers.tune(project['active_session_id'],project['receiver_stream_id'],{'frequency_hz':100000000})
        value=self.service.manual_tune(project['id'],dict(token=lease['token'],frequency_hz=100000000,mode='AM'))
        self.assertEqual(value['tuned']['mode'],'AM')
        with self.assertRaises(WorkerError):self.service.manual_tune(project['id'],dict(token='wrong',frequency_hz=100000000,mode='FM'))
        self.service.manual_return(project['id'],{'token':lease['token']})
        self.assertFalse(self.service.guard_status(project['id'])['manual'])
        self.service.disarm(project['id'],{})

    def test_narrow_search_pauses_wide_and_return_revalidates_without_harming_receiver(self):
        project=self.prepare(); sid=project['active_session_id']
        lease=self.service.manual_acquire(project['id'],{'role':'analyzer'})
        event=dict(id='search-event',project_id=project['id'],created_at='2026-01-01T00:00:00Z',center_frequency_hz=102000000)
        self.service.store.put('event',event)
        self.service.search_start(project['id'],dict(token=lease['token'],event_id=event['id'],span_hz=1000000))
        wait_until(lambda:len(self.service.search_status(project['id'])['series'])>0,'narrow sweep')
        result=self.service.search_status(project['id'])
        self.assertEqual(result['coverage'],'narrow');self.assertTrue(result['active'])
        self.service.search_mark(project['id'],dict(token=lease['token'],label='Door'))
        self.assertEqual(self.service.search_status(project['id'])['marks'][0]['label'],'Door')
        self.assertTrue(next(x for x in self.workers.list_workers(sid) if x['stream_id']==project['receiver_stream_id'])['alive'])
        self.service.manual_return(project['id'],{'token':lease['token']})
        wait_until(lambda:self.service.search_status(project['id'])['coverage']=='wide','validated broad coverage')
        self.service.disarm(project['id'],{})

    def test_expired_manual_owner_cannot_execute_queued_tune_but_can_return(self):
        project=self.prepare(); lease=self.service.manual_acquire(project['id'],{'role':'receiver'})
        runtime=self.service.runtime[project['id']];stream=project['receiver_stream_id']
        worker=self.workers._workers[(runtime['sid'],stream)]
        entered=threading.Event();release=threading.Event();errors=[]
        original=worker._execute_command
        def delayed(request):
            entered.set();release.wait(3);return original(request)
        def tune():
            try:self.service.manual_tune(project['id'],dict(token=lease['token'],frequency_hz=100000000,mode='AM'))
            except WorkerError as exc:errors.append(str(exc))
        with patch.object(worker,'_execute_command',side_effect=delayed):
            thread=threading.Thread(target=tune);thread.start();self.assertTrue(entered.wait(2))
            runtime['manual'][stream]['expires']=0;release.set();thread.join(3)
        self.assertTrue(errors);self.assertEqual(worker.adapter.mode,'AUTO')
        self.assertTrue(self.service.guard_status(project['id'])['manual'][0]['expired'])
        self.service.manual_return(project['id'],{'token':lease['token']})
        self.assertFalse(self.service.guard_status(project['id'])['manual'])
        self.service.disarm(project['id'],{})

    def test_history_cursor_same_timestamp_comparison_and_compatibility(self):
        from radioinspector.contracts import Sweep
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'],dict(name='Quiet',location='Room',confirm=True))
        item=sweep(20,session=self.session.id,stream=self.session.streams[0].id)
        for identifier in ['tie-a','tie-b','tie-c']:
            item.id=identifier;self.sessions.ingest_sweep(item)
        first=self.service.history(self.project['id'],{'limit':['1']})
        second=self.service.history(self.project['id'],{'limit':['1'],'after':[first['next_cursor']]})
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        result=self.service.history_comparison(self.project['id'],'tie-a')
        self.assertTrue(result['compatible']);self.assertEqual(result['items'][0]['delta_db'],0)
        item.id='different';item.settings['rbw_hz']=30000;self.sessions.ingest_sweep(item)
        result=self.service.history_comparison(self.project['id'],'different')
        self.assertFalse(result['compatible']);self.assertEqual(result['items'],[])
        self.assertIsNotNone(result['reason'])

    def test_disarm_stops_narrow_worker_and_preserves_narrow_sources(self):
        project=self.prepare();lease=self.service.manual_acquire(project['id'],{'role':'analyzer'})
        self.service.store.put('event',dict(id='narrow-event',project_id=project['id'],created_at='2026-01-01T00:00:00Z',center_frequency_hz=102000000))
        search=self.service.search_start(project['id'],dict(token=lease['token'],event_id='narrow-event',span_hz=1000000))
        wait_until(lambda:bool(self.service.search_status(project['id'])['series']),'narrow')
        self.service.disarm(project['id'],{})
        self.assertEqual(self.workers.active_count,0)
        history=self.service.history(project['id'],{'limit':['64']})
        self.assertTrue(any(x['session_id']==search['session_id'] for x in history['items']))

    def test_lost_narrow_worker_reports_paused_without_stopping_receiver(self):
        project=self.prepare();lease=self.service.manual_acquire(project['id'],{'role':'analyzer'})
        self.service.store.put('event',dict(id='lost-narrow',project_id=project['id'],created_at='2026-01-01T00:00:00Z',center_frequency_hz=102000000))
        search=self.service.search_start(project['id'],dict(token=lease['token'],event_id='lost-narrow',span_hz=1000000))
        self.workers.stop_session(search['session_id'])
        wait_until(lambda:not self.service.search_status(project['id'])['active'],'narrow failure visible')
        self.assertEqual(self.service.search_status(project['id'])['coverage'],'paused')
        receiver=next(x for x in self.workers.list_workers(project['active_session_id']) if x['stream_id']==project['receiver_stream_id'])
        self.assertTrue(receiver['alive']);self.service.disarm(project['id'],{})

    def test_search_poll_during_return_never_loses_its_search_snapshot(self):
        project=self.prepare();lease=self.service.manual_acquire(project['id'],{'role':'analyzer'})
        self.service.store.put('event',dict(id='poll-return',project_id=project['id'],created_at='2026-01-01T00:00:00Z',center_frequency_hz=102000000))
        self.service.search_start(project['id'],dict(token=lease['token'],event_id='poll-return',span_hz=1000000))
        entered=threading.Event();release=threading.Event();errors=[];results=[]
        original=self.service._search_tick
        def delayed(identifier,runtime):
            if threading.current_thread().name=='research-poller':entered.set();release.wait(2)
            return original(identifier,runtime)
        def poll():
            try:results.append(self.service.search_status(project['id']))
            except Exception as exc:errors.append(exc)
        with patch.object(self.service,'_search_tick',side_effect=delayed):
            reader=threading.Thread(target=poll,name='research-poller');reader.start();self.assertTrue(entered.wait(2))
            returning=threading.Thread(target=lambda:self.service.manual_return(project['id'],{'token':lease['token']}));returning.start()
            deadline=time.monotonic()+.3
            while self.service.project(project['id'])['coverage']!='wide' and time.monotonic()<deadline:time.sleep(.005)
            release.set();reader.join(3);returning.join(3)
        self.assertFalse(errors);self.assertEqual(len(results),1)
        wait_until(lambda:self.service.project(project['id'])['coverage']=='wide','return coverage')
        self.service.disarm(project['id'],{})

    def test_history_http_scope_bounds_and_manual_conflict_preserve_archive(self):
        from radioinspector.web.server import create_server
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        server=create_server(host='127.0.0.1',port=0,sessions=self.sessions,workers=self.workers)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(method,suffix,body=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            connection.request(method,'/api/v1/monitoring-projects/'+self.project['id']+suffix,
                json.dumps(body) if body is not None else None,{'Content-Type':'application/json'})
            response=connection.getresponse();data=json.loads(response.read());connection.close()
            return response.status,data
        try:
            status,data=request('GET','/history?limit=2')
            self.assertEqual(status,200);self.assertEqual(len(data['data']['items']),2)
            self.assertEqual(request('GET','/history?limit=65')[0],400)
            self.assertEqual(request('GET','/history?limit=2&limit=3')[0],400)
            self.assertEqual(request('GET','/history/missing')[0],404)
            self.assertEqual(request('POST','/manual/acquire',{'role':'receiver'})[0],409)
            self.assertEqual(len(self.sessions.list_sweeps(self.session.id)),20)
            self.assertEqual(self.workers.active_count,0)
        finally:server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':unittest.main()
