"""The production Python listener participates in owned runtime shutdown."""
import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from radioinspector.runtime_bridge import RuntimeBridgeError
from radioinspector.web.server import create_server
from tests.test_platform import session_payload
from tests.test_runtime_bridge import TOKEN, INSTANCE, STATUS, SHUTDOWN, shutdown_payload


class RuntimeServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.env = dict(ORI_RUNTIME_CONTROL_TOKEN=TOKEN, ORI_RUNTIME_INSTANCE_ID=INSTANCE,
                        ORI_RUNTIME_DATA_DIR=str(self.directory))
        with patch.dict(os.environ,self.env):
            self.server=create_server(host='127.0.0.1',port=0,session_database=str(self.directory/'archive.sqlite'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self,method,path,payload=None,*,authenticated=True):
        headers={'Authorization':'Bearer '+TOKEN} if authenticated else {}
        if payload is not None:headers['Content-Type']='application/json'
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        try:
            connection.request(method,path,None if payload is None else json.dumps(payload),headers)
            response=connection.getresponse()
            return response.status,json.loads(response.read())
        finally:
            connection.close()

    def test_private_status_is_authenticated_and_not_a_public_contract_envelope(self):
        self.assertEqual(self.request('GET',STATUS,authenticated=False)[0],401)
        self.assertTrue(self.server.monitoring.startup_recovery_complete.wait(2))
        status,value=self.request('GET',STATUS)
        self.assertEqual(status,200)
        self.assertEqual(value['instance_id'],INSTANCE)
        self.assertTrue(value['writer_owned'])
        self.assertTrue(value['ready'])
        self.assertNotIn('contract',value)

    def test_writer_exclusion_precedes_any_second_archive_open(self):
        with patch.dict(os.environ,self.env), patch('radioinspector.web.server.SQLiteSessionStore',side_effect=AssertionError('Archive opened before writer exclusion')):
            with self.assertRaises(RuntimeBridgeError):
                create_server(host='127.0.0.1',port=0,session_database=str(self.directory/'archive.sqlite'))

    def test_supervised_archive_cannot_alias_a_nested_data_owner(self):
        nested=self.directory/'nested';nested.mkdir()
        from radioinspector.runtime_bridge import RuntimeBridge
        with self.assertRaises(RuntimeBridgeError) as rejected:
            RuntimeBridge.from_environment(str(nested/'archive.sqlite'),environ=self.env)
        self.assertEqual(rejected.exception.code,'runtime_database_outside_data_directory')

    def test_mutation_admission_stops_without_rejecting_public_reads(self):
        bridge=self.server.runtime_bridge
        status,_=bridge.control_response('POST',SHUTDOWN,peer=('127.0.0.1',1),
                                        authorization=['Bearer '+TOKEN],payload=shutdown_payload())
        self.assertEqual(status,202)
        before=self.server.session_service.events()
        status,value=self.request('POST','/api/v1/sessions',session_payload(),authenticated=False)
        self.assertEqual(status,503)
        self.assertEqual(value['error']['code'],'runtime_quiescing')
        self.assertEqual(self.server.session_service.events(),before)
        self.assertEqual(self.request('GET','/api/v1/health',authenticated=False)[0],200)
        for tail in ('screen','screen/','screen////'):
            self.assertEqual(self.request('GET','/api/v1/sessions/missing/streams/missing/instrument/'+tail,authenticated=False)[0],503)
        bridge.response_not_flushed()
        self.assertEqual(self.request('POST','/api/v1/sessions',session_payload(),authenticated=False)[0],201)


class RuntimeLauncherTests(unittest.TestCase):
    def test_authenticated_shutdown_exits_owned_launcher_and_keeps_committed_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary).resolve()
            ready=directory/'ready'
            environment=dict(os.environ,ORI_RUNTIME_CONTROL_TOKEN=TOKEN,
                             ORI_RUNTIME_INSTANCE_ID=INSTANCE,ORI_RUNTIME_DATA_DIR=str(directory))
            process=subprocess.Popen([sys.executable,'-m','radioinspector.launcher','--data-dir',str(directory),
                                      '--port','0','--ready-file',str(ready),'--stdin-lifeline'],
                                     env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+15
                while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
                if not ready.is_file() and process.poll() is not None:
                    _stdout,stderr=process.communicate(timeout=5)
                    self.fail('Owned launcher exited before readiness: '+stderr.decode(errors='replace'))
                self.assertTrue(ready.is_file(),'Owned launcher did not become ready')
                from urllib.request import Request,urlopen
                base=ready.read_text().strip()
                request=Request(base+'/api/v1/sessions',data=json.dumps(session_payload()).encode(),
                                headers={'Content-Type':'application/json'})
                with urlopen(request,timeout=5) as response:identifier=json.load(response)['data']['id']
                request=Request(base+SHUTDOWN,data=json.dumps(shutdown_payload()).encode(),
                                headers={'Content-Type':'application/json','Authorization':'Bearer '+TOKEN})
                with urlopen(request,timeout=5) as response:self.assertEqual(response.status,202)
                stdout,stderr=process.communicate(timeout=15)
                self.assertEqual(process.returncode,0,stderr.decode())
                self.assertNotIn(TOKEN.encode(),stdout+stderr)
                self.assertFalse(ready.exists())
                from radioinspector.session_store import SQLiteSessionStore
                store=SQLiteSessionStore(str(directory/'sessions.sqlite3'))
                try:self.assertEqual(store.get_session(identifier).id,identifier)
                finally:store.close()
            finally:
                if process.poll() is None:process.kill()
                process.communicate(timeout=5)
