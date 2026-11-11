"""Synthetic own processes/HTTP/SQLite only; no device or deployed service."""
import gc
import http.client
import http.server
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import weakref
from contextlib import ExitStack, closing

from radioinspector.runtime_bridge import RuntimeBridge, RuntimeBridgeError
from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.monitoring.service import MonitoringService
from tests.test_device_workers import simulated_session_payload
from tests.test_monitoring_detector import sweep

TOKEN = 'A' * 43
INSTANCE = 'synthetic-runtime-instance'
STATUS = '/internal/runtime/v1/status'
SHUTDOWN = '/internal/runtime/v1/shutdown'


def shutdown_payload(reason='shutdown', acknowledge=False):
    return dict(version=1, preserve_resume_requested=True, reason=reason,
                acknowledge_coverage_gap=acknowledge)


class HttpFixture:
    """Minimal consuming handler, real archive and monitoring, coordinated lifecycle."""
    def __init__(self, test, directory, env):
        self.bridge = RuntimeBridge.from_environment(str(directory/'archive.sqlite'), environ=env)
        self.sessions = SessionService(store=SQLiteSessionStore(str(directory/'archive.sqlite')))
        self.workers = DeviceWorkerManager(self.sessions)
        self.monitoring = MonitoringService(self.sessions, self.workers)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.failure = None
        self.mutate = lambda: None
        fixture = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def send_json(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
            def dispatch(self):
                try:
                    authorization=self.headers.get_all('Authorization', [])
                    if self.path.startswith('/internal/runtime/'):
                        fixture.bridge.authorize_control(self.client_address, authorization)
                        payload=None
                        if self.command=='POST':
                            lengths=self.headers.get_all('Content-Length', [])
                            if self.headers.get_all('Transfer-Encoding') or len(lengths)!=1 or not lengths[0].isdigit() or int(lengths[0])>4096:
                                raise RuntimeBridgeError(400,'invalid_framing')
                            payload=self.rfile.read(int(lengths[0]))
                        status, value=fixture.bridge.control_response(self.command,self.path,
                            peer=self.client_address,authorization=authorization,payload=payload)
                        self.send_json(status,value)
                        if status==202:fixture.bridge.after_response_flushed()
                    else:
                        length=int(self.headers.get('Content-Length','0'))
                        self.rfile.read(length)
                        with fixture.bridge.mutation():
                            fixture.mutate()
                            self.send_json(200,{'ok':True})
                except RuntimeBridgeError as error:
                    self.send_json(error.status,{'error':error.code})
            do_GET=dispatch
            do_POST=dispatch
        self.httpd=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.httpd.daemon_threads=True
        self.bridge.attach(self.httpd,self.monitoring)
        self.monitoring.start()
        def serve():
            try:
                self.httpd.serve_forever(poll_interval=.01)
            finally:
                try:
                    self.monitoring.close()
                    self.workers.close()
                    self.sessions.close()
                except BaseException:
                    self.failure=sys.exc_info()[1]
                    self.bridge.__exit__(*sys.exc_info())
                else:
                    self.bridge.__exit__(None,None,None)
                finally:
                    self.httpd.server_close()
                    self.closed.set()
        self.thread=threading.Thread(target=serve,daemon=True)
        self.thread.start()
        test.addCleanup(self.close)
    def close(self):
        self.release.set()
        if self.thread.is_alive():self.httpd.shutdown()
        self.thread.join(5)
        if self.failure is not None:
            # An explicit safe cleanup retry for test resources; never auto-release on a failed close.
            self.sessions.close()
            self.bridge.release_after_archive_closed()
    def request(self, method='GET', target=STATUS, payload=None, headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.httpd.server_port,timeout=3)
        try:
            connection.request(method,target,body=json.dumps(payload).encode() if payload is not None else None,
                headers=headers or {'Authorization':'Bearer '+TOKEN})
            response=connection.getresponse()
            return response.status,json.loads(response.read())
        finally:connection.close()
    def arm(self):
        session=self.sessions.create_session(simulated_session_payload())
        project=self.monitoring.create(dict(name='Synthetic',site='Fixture',session_id=session.id))
        for index in range(20):self.sessions.ingest_sweep(sweep(index,session=session.id,stream=session.streams[0].id))
        self.monitoring.create_baseline(project['id'],dict(name='Quiet',location='Fixture',confirm=True))
        self.monitoring.update_settings(project['id'],{'autostart':True})
        return self.monitoring.arm(project['id'],{})


class RuntimeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory=Path(self.temporary.name)
        self.database=self.directory/'archive.sqlite'
        self.env=dict(ORI_RUNTIME_CONTROL_TOKEN=TOKEN,ORI_RUNTIME_INSTANCE_ID=INSTANCE,
                      ORI_RUNTIME_DATA_DIR=str(self.directory))
    def bridge(self, env=None):
        bridge=RuntimeBridge.from_environment(str(self.database),environ=self.env if env is None else env)
        self.addCleanup(bridge.release_after_archive_closed)
        return bridge
    def control(self, bridge, payload=None, method='POST', path=SHUTDOWN):
        return bridge.control_response(method,path,peer=('127.0.0.1',1),
            authorization=['Bearer '+TOKEN],payload=payload)
    def test_disabled_environment_is_inert_and_partial_configuration_fails_closed(self):
        self.assertIsNone(RuntimeBridge.from_environment(str(self.database),environ={}))
        for env in ({'ORI_RUNTIME_CONTROL_TOKEN':TOKEN},{'ORI_RUNTIME_INSTANCE_ID':INSTANCE},
                    {'ORI_RUNTIME_DATA_DIR':str(self.directory)}):
            with self.subTest(keys=list(env)):
                with self.assertRaises(RuntimeBridgeError):RuntimeBridge.from_environment(str(self.database),environ=env)
        self.assertFalse(self.database.exists())
    def test_lease_is_acquired_before_any_archive_file_and_conflicting_context_is_refused(self):
        bridge=self.bridge()
        self.assertFalse(self.database.exists())
        with self.assertRaises(RuntimeBridgeError):RuntimeBridge.from_environment(str(self.database),environ=self.env)
        self.assertTrue(bridge.status()['writer_owned'])
        bridge.release_after_archive_closed()
        self.bridge()
    def test_real_subprocess_writer_lock_is_exclusive_and_releases_on_exit(self):
        code='from radioinspector.runtime_bridge import RuntimeBridge; import sys; b=RuntimeBridge.from_environment(sys.argv[1]); print("held",flush=True); sys.stdin.readline(); b.release_after_archive_closed()'
        environment={key:value for key,value in os.environ.items() if not key.startswith('ORI_RUNTIME_')}
        environment.update(self.env)
        child=subprocess.Popen((sys.executable,'-c',code,str(self.database)),env=environment,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            messages=queue.Queue()
            reader=threading.Thread(target=lambda:messages.put(child.stdout.readline()),daemon=True);reader.start()
            self.assertEqual(messages.get(timeout=5),'held\n')
            with self.assertRaises(RuntimeBridgeError):self.bridge()
            child.communicate('\n',timeout=5)
            self.assertEqual(child.returncode,0)
            self.bridge()
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
    def test_authentication_rejects_duplicate_stale_tokens_and_non_loopback_peers(self):
        bridge=self.bridge()
        bridge.authorize_control(('127.0.0.1',3),['Bearer '+TOKEN])
        bridge.authorize_control(('::1',3,0,0),['Bearer '+TOKEN])
        for peer,headers in [(('192.168.1.2',3),['Bearer '+TOKEN]),(('localhost',3),['Bearer '+TOKEN]),
            (('127.0.0.1',3),[]),(('127.0.0.1',3),['Bearer '+TOKEN]*2),
            (('127.0.0.1',3),['Bearer '+'B'*43]),(('127.0.0.1',3),['Bearer '+TOKEN+', Bearer '+TOKEN])]:
            with self.subTest(peer=peer,header_count=len(headers)):
                with self.assertRaises(RuntimeBridgeError):bridge.authorize_control(peer,headers)
        self.assertEqual(bridge.status()['instance_id'],INSTANCE)
        self.assertEqual(bridge.status()['pid'],os.getpid())
    def test_status_waits_for_actual_startup_recovery_completion_without_claiming_hardware(self):
        bridge=self.bridge()
        sessions=SessionService(store=SQLiteSessionStore(str(self.database)))
        workers=DeviceWorkerManager(sessions);monitoring=MonitoringService(sessions,workers)
        self.addCleanup(sessions.close);self.addCleanup(workers.close);self.addCleanup(monitoring.close)
        bridge.attach(object(),monitoring)
        self.assertFalse(bridge.status()['ready']);self.assertFalse(bridge.status()['preflight_complete'])
        monitoring.start()
        self.assertTrue(monitoring.startup_recovery_complete.wait(3))
        status=bridge.status()
        self.assertTrue(status['ready']);self.assertTrue(status['preflight_complete'])
        self.assertFalse(status['guard_armed']);self.assertEqual(workers.active_count,0)
    def test_recovery_failure_finishes_attempts_but_never_reports_ready(self):
        bridge=self.bridge()
        sessions=SessionService(store=SQLiteSessionStore(str(self.database)))
        workers=DeviceWorkerManager(sessions);monitoring=MonitoringService(sessions,workers)
        self.addCleanup(sessions.close);self.addCleanup(workers.close);self.addCleanup(monitoring.close)
        bridge.attach(object(),monitoring)
        sessions.close();monitoring.start()
        self.assertTrue(monitoring.startup_recovery_complete.wait(3))
        self.assertFalse(bridge.status()['ready']);self.assertFalse(bridge.status()['preflight_complete'])
        self.assertIsNotNone(monitoring.startup_recovery_error)
    def test_shutdown_validation_is_strict_and_does_not_quiesce_on_bad_payload(self):
        bridge=self.bridge()
        for payload in [None,[],{},dict(shutdown_payload(),version=True),dict(shutdown_payload(),version=2),
            dict(shutdown_payload(),preserve_resume_requested=False),dict(shutdown_payload(),reason='disarm'),
            dict(shutdown_payload(),acknowledge_coverage_gap=1),dict(shutdown_payload(),instance_id='stale'),
            b'{"version":1,"version":1,"preserve_resume_requested":true,"reason":"shutdown"}']:
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(RuntimeBridgeError):self.control(bridge,payload)
            with bridge.mutation():pass
        with self.assertRaises(RuntimeBridgeError):self.control(bridge,method='GET',path=STATUS+'?token='+TOKEN)
    def test_accepted_shutdown_does_not_start_before_response_flush(self):
        fixture=HttpFixture(self,self.directory,self.env)
        status,_=self.control(fixture.bridge,shutdown_payload())
        self.assertEqual(status,202)
        self.assertFalse(fixture.closed.wait(.05))
        with self.assertRaises(RuntimeBridgeError):
            with fixture.bridge.mutation():pass
        fixture.bridge.after_response_flushed()
        self.assertTrue(fixture.closed.wait(3));self.assertIsNone(fixture.failure)
    def test_inflight_mutation_drains_and_later_post_is_rejected(self):
        fixture=HttpFixture(self,self.directory,self.env)
        fixture.sessions._store._connection.execute('CREATE TABLE synthetic_commits (value TEXT)')
        def mutate():
            fixture.entered.set();fixture.release.wait(3)
            with fixture.sessions._store._lock:
                fixture.sessions._store._connection.execute("INSERT INTO synthetic_commits VALUES ('latest')")
                fixture.sessions._store._connection.commit()
        fixture.mutate=mutate
        replies=[]
        admitted=threading.Thread(target=lambda:replies.append(fixture.request('POST','/mutate',{})))
        admitted.start();self.addCleanup(admitted.join,4)
        self.assertTrue(fixture.entered.wait(2))
        self.assertEqual(fixture.request('POST',SHUTDOWN,shutdown_payload())[0],202)
        self.assertEqual(fixture.request('POST','/mutate',{})[0],503)
        self.assertFalse(fixture.closed.is_set())
        fixture.release.set();admitted.join(3)
        self.assertEqual(replies[0][0],200)
        self.assertTrue(fixture.closed.wait(3))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT value FROM synthetic_commits').fetchall(),[('latest',)])
    def test_armed_cutover_requires_acknowledgment_and_close_preserves_opt_in_intent(self):
        fixture=HttpFixture(self,self.directory,self.env)
        project=fixture.arm()
        self.assertEqual(fixture.request('POST',SHUTDOWN,shutdown_payload('cutover'))[0],409)
        self.assertTrue(fixture.bridge.status()['guard_armed'])
        self.assertEqual(fixture.request('POST',SHUTDOWN,shutdown_payload('cutover',True))[0],202)
        self.assertTrue(fixture.closed.wait(5));self.assertIsNone(fixture.failure)
        with closing(sqlite3.connect(self.database)) as connection:
            project=json.loads(connection.execute("SELECT payload FROM monitoring_records WHERE kind='project' AND id=?",(project['id'],)).fetchone()[0])
        self.assertTrue(project['resume_requested']);self.assertTrue(project['autostart'])
    def test_previously_admitted_arm_aborts_unacknowledged_cutover_after_drain(self):
        fixture=HttpFixture(self,self.directory,self.env)
        def arm_later():
            fixture.entered.set();fixture.release.wait(3);fixture.arm()
        fixture.mutate=arm_later
        replies=[]
        admitted=threading.Thread(target=lambda:replies.append(fixture.request('POST','/arm',{})))
        admitted.start();self.addCleanup(admitted.join,4)
        self.assertTrue(fixture.entered.wait(2))
        self.assertEqual(fixture.request('POST',SHUTDOWN,shutdown_payload('cutover'))[0],202)
        fixture.release.set();admitted.join(4)
        deadline=time.monotonic()+3
        while not fixture.bridge.status()['ready'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(fixture.bridge.status()['guard_armed'])
        self.assertTrue(fixture.bridge.status()['ready']);self.assertFalse(fixture.closed.is_set())
        self.assertEqual(fixture.request('POST',SHUTDOWN,shutdown_payload())[0],202)
        self.assertTrue(fixture.closed.wait(5))
    def test_cleanup_failure_keeps_the_writer_lease_even_after_context_gc(self):
        bridge=RuntimeBridge.from_environment(str(self.database),environ=self.env)
        reference=weakref.ref(bridge)
        connection=sqlite3.connect(self.database)
        stack=ExitStack();stack.enter_context(bridge)
        def incomplete_close():raise RuntimeError('synthetic archive still open')
        stack.callback(incomplete_close)
        with self.assertRaises(RuntimeError):stack.close()
        del stack,bridge;gc.collect()
        self.assertIsNotNone(reference())
        with self.assertRaises(RuntimeBridgeError):self.bridge()
        connection.execute('CREATE TABLE still_open (value TEXT)');connection.commit();connection.close()
        retained=reference()
        with self.assertRaises(RuntimeBridgeError):retained.close()
        retained.release_after_archive_closed()
        retained.close()
        self.bridge()

    def test_private_paths_and_database_scope_are_checked_before_lease_acquisition(self):
        outside=self.directory.parent/'synthetic-other-archive.sqlite'
        with self.assertRaises(RuntimeBridgeError):RuntimeBridge.from_environment(str(outside),environ=self.env)
        if os.name != 'nt':
            shared=self.directory/'shared';shared.mkdir();shared.chmod(0o777)
            with self.assertRaises(RuntimeBridgeError):RuntimeBridge.from_environment(str(shared/'archive.sqlite'),environ=dict(self.env,ORI_RUNTIME_DATA_DIR=str(shared)))
            real=self.directory/'real';real.mkdir(mode=0o700)
            link=self.directory/'linked';link.symlink_to(real,target_is_directory=True)
            with self.assertRaises(RuntimeBridgeError):RuntimeBridge.from_environment(str(link/'archive.sqlite'),environ=dict(self.env,ORI_RUNTIME_DATA_DIR=str(link)))
        fallback=dict(self.env);fallback.pop('ORI_RUNTIME_DATA_DIR')
        bridge=self.bridge(fallback)
        self.assertEqual(bridge.data_directory,self.directory)
        if os.name != 'nt':
            self.assertEqual((self.directory/'.ori-runtime').stat().st_mode & 0o777,0o700)
            self.assertEqual((self.directory/'.ori-runtime/writer.lock').stat().st_mode & 0o777,0o600)
        self.assertNotIn(TOKEN,json.dumps(bridge.status()))

    def test_http_duplicate_auth_and_malformed_framing_reject_before_quiesce(self):
        fixture=HttpFixture(self,self.directory,self.env)
        for duplicate in (True,False):
            connection=http.client.HTTPConnection('127.0.0.1',fixture.httpd.server_port,timeout=3)
            try:
                connection.putrequest('POST',SHUTDOWN)
                connection.putheader('Authorization','Bearer '+TOKEN)
                if duplicate:connection.putheader('Authorization','Bearer '+TOKEN)
                # No Content-Length: duplicate authentication must win before body framing.
                connection.endheaders()
                response=connection.getresponse();response.read()
                self.assertEqual(response.status,401 if duplicate else 400)
            finally:connection.close()
            with fixture.bridge.mutation():pass

    def test_restarted_identity_does_not_accept_the_old_control_token(self):
        old=self.bridge();old.release_after_archive_closed()
        replacement=self.bridge(dict(self.env,ORI_RUNTIME_CONTROL_TOKEN='B'*43,ORI_RUNTIME_INSTANCE_ID='replacement'))
        with self.assertRaises(RuntimeBridgeError):replacement.authorize_control(('127.0.0.1',1),['Bearer '+TOKEN])
        replacement.authorize_control(('127.0.0.1',1),['Bearer '+'B'*43])
        self.assertEqual(replacement.status()['instance_id'],'replacement')

    def test_failed_202_flush_reopens_admission_without_stopping_the_owner(self):
        fixture=HttpFixture(self,self.directory,self.env)
        self.assertEqual(self.control(fixture.bridge,shutdown_payload())[0],202)
        fixture.bridge.response_not_flushed()
        with fixture.bridge.mutation():pass
        fixture.bridge.after_response_flushed()
        self.assertFalse(fixture.closed.is_set())

    def test_busy_guard_state_returns_prompt_failure_without_quiescing(self):
        fixture=HttpFixture(self,self.directory,self.env)
        locked=threading.Event();release=threading.Event()
        def hold_state():
            with fixture.monitoring.lock:locked.set();release.wait(3)
        holder=threading.Thread(target=hold_state);holder.start()
        self.addCleanup(holder.join,4);self.addCleanup(release.set)
        self.assertTrue(locked.wait(2))
        replies=queue.Queue()
        def request():
            try:replies.put(self.control(fixture.bridge,shutdown_payload()))
            except RuntimeBridgeError as error:replies.put(error.status)
        requester=threading.Thread(target=request);requester.start()
        try:
            try:result=replies.get(timeout=.7)
            except queue.Empty:self.fail('private control blocked behind long-running guard work')
            self.assertEqual(result,503)
        finally:release.set();holder.join(4);requester.join(4)
        with fixture.bridge.mutation():pass

    def test_unexpected_startup_thread_exit_never_becomes_ready(self):
        bridge=self.bridge()
        sessions=SessionService(store=SQLiteSessionStore(str(self.database)))
        workers=DeviceWorkerManager(sessions);monitoring=MonitoringService(sessions,workers)
        self.addCleanup(sessions.close);self.addCleanup(workers.close);self.addCleanup(monitoring.close)
        bridge.attach(object(),monitoring)
        def unexpected_exit():raise SystemExit(0)
        monitoring._recover_projects=unexpected_exit
        monitoring.start()
        self.assertTrue(monitoring.startup_recovery_complete.wait(3))
        self.assertFalse(bridge.status()['ready'])
        self.assertFalse(bridge.status()['preflight_complete'])


if __name__=='__main__':unittest.main()
