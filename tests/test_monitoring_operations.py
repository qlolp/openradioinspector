import hashlib
import hmac
import http.server
import json
import threading
import unittest
import urllib.error
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from radioinspector.contracts import utc_now
from tests import test_monitoring_guard as guard_tests
from tests.test_monitoring_detector import sweep


class OperationalTests(unittest.TestCase):
    setUp=guard_tests.GuardTests.setUp

    def test_storage_checks_space_before_device_start_and_never_deletes(self):
        with patch('radioinspector.monitoring.operations.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            report=self.service.storage_report(self.project['id'],probe=True)
        self.assertFalse(report['ready']);self.assertFalse(report['automatic_deletion'])
        self.assertEqual(self.workers.active_count,0)

    def test_settings_never_echo_webhook_secret_and_invalid_request_is_atomic(self):
        value=self.service.update_settings(self.project['id'],dict(webhook=dict(enabled=True,url='http://127.0.0.1/hook',secret='private-fixture-secret')))
        self.assertTrue(value['webhook']['secret_configured']);self.assertNotIn('private-fixture-secret',json.dumps(value))
        before=self.service.project(self.project['id'])
        with self.assertRaises(ValueError):self.service.update_settings(self.project['id'],dict(autostart=True,archive={'minimum_free_bytes':-1}))
        self.assertEqual(self.service.project(self.project['id']),before)

    def test_persistent_outbox_stable_id_signature_bounded_retries_and_ack(self):
        captures=[]
        class Hook(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']));captures.append((dict(self.headers),body))
                self.send_response(503 if len(captures)<2 else 204);self.end_headers()
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Hook)
        thread=threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},daemon=True);thread.start()
        self.addCleanup(thread.join,3);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        self.service.update_settings(self.project['id'],dict(webhook=dict(enabled=True,url=f'http://127.0.0.1:{server.server_port}/hook',secret='fixture-secret')))
        project=self.service.project(self.project['id']);event=dict(id='event',kind='impulse',center_frequency_hz=100000000)
        identifier=self.service.notify_event(project,event)
        self.assertEqual(identifier,self.service.notify_event(project,event))
        self.assertEqual(len(self.service.alerts(project['id'],{})['items']),1)
        now=utc_now();self.assertTrue(self.service.deliver_one(now))
        self.assertEqual(self.service.alerts(project['id'],{})['items'][0]['delivery']['state'],'pending')
        self.assertFalse(self.service.deliver_one(now))
        self.assertTrue(self.service.deliver_one(now+timedelta(minutes=1)))
        alert=self.service.alerts(project['id'],{})['items'][0]
        self.assertEqual(alert['delivery']['state'],'sent');self.assertEqual(alert['delivery']['attempts'],2)
        headers,body=captures[0]
        self.assertEqual(headers['X-Openradioinspector-Notification-Id'],identifier)
        self.assertEqual(headers['X-Openradioinspector-Signature'],'sha256='+hmac.new(b'fixture-secret',body,hashlib.sha256).hexdigest())
        self.assertEqual(captures[0][1],captures[1][1])
        self.service.acknowledge_alert(project['id'],identifier,{})
        self.assertIsNotNone(self.service.alerts(project['id'],{})['items'][0]['acknowledged_at'])

    def test_restart_does_not_replay_stale_investigations_and_autostart_is_opt_in(self):
        project=self.service.project(self.project['id']);project.update(state='armed',coverage='wide',resume_requested=True)
        self.service.store.put('project',project)
        event=dict(id='stale',project_id=project['id'],created_at='2026-01-01T00:00:00Z',analysis={'state':'queued'})
        self.service.store.put('event',event)
        with patch.object(self.service,'arm') as arm:self.service._recover_projects();arm.assert_not_called()
        self.assertEqual(self.service.project(project['id'])['state'],'interrupted')
        self.assertEqual(self.service.store.get('event','stale')['analysis']['state'],'interrupted')
        self.assertEqual(self.workers.active_count,0)

    def ready_project(self):
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'],dict(name='Quiet',location='Room',confirm=True))
        return self.service.project(self.project['id'])

    def test_opted_in_restart_creates_fresh_run_but_disarm_removes_restart_intent(self):
        project=self.ready_project();project.update(state='armed',autostart=True,resume_requested=True)
        self.service.store.put('project',project)
        with patch.object(self.service,'_launch_guard'):
            self.service._recover_projects()
        resumed=self.service.project(project['id'])
        self.assertEqual(resumed['state'],'armed');self.assertNotEqual(resumed['active_session_id'],project['active_session_id'])
        self.service.disarm(project['id'],{})
        self.assertFalse(self.service.project(project['id'])['resume_requested'])

    def test_degraded_restart_obeys_opt_in_and_does_not_leave_stale_coverage(self):
        project=self.ready_project()
        for enabled in (False,True):
            project=self.service.project(project['id']);project.update(state='degraded',coverage='wide',autostart=enabled,resume_requested=False)
            self.service.store.put('project',project)
            with patch.object(self.service,'_launch_guard'):
                self.service._recover_projects()
            recovered=self.service.project(project['id'])
            self.assertEqual(recovered['state'],'armed' if enabled else 'interrupted')
            if enabled:self.service.disarm(project['id'],{})

    def test_missing_archive_budget_rejects_arm_without_starting_devices(self):
        project=self.ready_project()
        with patch('radioinspector.monitoring.operations.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaises(ValueError):self.service.arm(project['id'],{})
        self.assertEqual(self.workers.active_count,0)
        self.assertEqual(len(self.sessions.list_sweeps(self.session.id)),20)

    def test_readonly_probe_rolls_back_and_reports_unavailable_write(self):
        db=self.sessions._store._connection
        db.execute('PRAGMA query_only=ON')
        try:self.assertFalse(self.service.storage_report(self.project['id'],probe=True)['ready'])
        finally:db.execute('PRAGMA query_only=OFF')
        self.assertFalse(db.in_transaction)

    def test_final_delivery_crash_becomes_terminal_without_unbounded_retry(self):
        self.service.update_settings(self.project['id'],dict(webhook=dict(enabled=True,url='http://127.0.0.1/hook',secret='fixture-secret')))
        identifier=self.service.notify_event(self.service.project(self.project['id']),dict(id='last',kind='impulse',center_frequency_hz=100000000))
        with self.service.store.transaction():
            self.service.store.db.execute("UPDATE monitoring_notifications SET state='delivering',attempts=5,lease_until='2000-01-01T00:00:00.000000Z' WHERE id=?",(identifier,))
        with patch.object(self.service._delivery_opener,'open') as opened:
            self.service.deliver_one();opened.assert_not_called()
        alert=self.service.alerts(self.project['id'],{})['items'][0]
        self.assertEqual(alert['delivery']['state'],'failed');self.assertEqual(alert['delivery']['attempts'],5)
        self.assertEqual(alert['delivery']['last_error'],'last_attempt_interrupted')

    def test_late_delivery_cannot_overwrite_newer_success(self):
        self.service.update_settings(self.project['id'],dict(webhook=dict(enabled=True,url='http://127.0.0.1/hook',secret='fixture-secret')))
        identifier=self.service.notify_event(self.service.project(self.project['id']),dict(id='overlap',kind='impulse',center_frequency_hz=100000000))
        with self.service.store.transaction():self.service.store.db.execute('UPDATE monitoring_notifications SET attempts=3 WHERE id=?',(identifier,))
        entered=threading.Event();release=threading.Event();calls=[]
        class Response:
            status=204
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return b''
        def open_request(*args,**kwargs):
            calls.append(True)
            if len(calls)==1:
                entered.set();release.wait(3);raise urllib.error.HTTPError('private-url',503,'fixture',{},None)
            return Response()
        now=utc_now()
        with patch.object(self.service._delivery_opener,'open',side_effect=open_request):
            first=threading.Thread(target=lambda:self.service.deliver_one(now));first.start();self.assertTrue(entered.wait(2))
            self.assertTrue(self.service.deliver_one(now+timedelta(seconds=31)))
            release.set();first.join(3)
        alert=self.service.alerts(self.project['id'],{})['items'][0]
        self.assertEqual(alert['delivery']['state'],'sent');self.assertEqual(alert['delivery']['attempts'],5)

    def test_write_failure_is_critical_even_when_both_roles_are_manual(self):
        from tests.test_monitoring_research import ResearchTests
        from tests.test_device_workers import wait_until
        project=ResearchTests.prepare(self)
        self.service.manual_acquire(project['id'],{'role':'receiver'})
        self.service.manual_acquire(project['id'],{'role':'analyzer'})
        db=self.sessions._store._connection;db.execute('PRAGMA query_only=ON')
        try:
            wait_until(lambda:bool(self.service.guard_status(project['id'])['error']),'archive failure visible')
            status=self.service.guard_status(project['id']);self.assertFalse(status['active'])
            self.assertEqual(status['project']['state'],'error')
            alerts=self.service.alerts(project['id'],{})['items']
            self.assertTrue(any(x['severity']=='critical' for x in alerts))
        finally:db.execute('PRAGMA query_only=OFF')
        self.service.disarm(project['id'],{})

if __name__=='__main__':unittest.main()
