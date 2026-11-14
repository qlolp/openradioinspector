import hashlib
import http.client
import importlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.serial_instruments import TinySASerialAdapter
from radioinspector.web.server import create_server
from tests.test_serial_instruments import serial_session
from tests.tinysa_control_fixture import UltraWire


class InstrumentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.client_key=uuid.uuid4().hex
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'archive.sqlite')
        self.sessions=SessionService(store=SQLiteSessionStore(self.path));self.addCleanup(self.sessions.close)
        self.wires=[]
        def factory(stream,**kwargs):
            wire=UltraWire({'capture':b'\xf8\0'*153600});self.wires.append(wire)
            return TinySASerialAdapter(stream,transport=wire)
        self.workers=DeviceWorkerManager(self.sessions);self.addCleanup(self.workers.close)
        self.factory_patch=patch('radioinspector.serial_instruments.create_device_adapter',factory)
        self.factory_patch.start();self.addCleanup(self.factory_patch.stop)
        self.session=self.sessions.create_session(serial_session())
        self.stream=self.session.streams[0]
        self.sessions.start_session(self.session.id)
        self.workers.start_stream(self.session.id,self.stream.id)
        self.server=create_server(host='127.0.0.1',port=0,sessions=self.sessions,workers=self.workers)
        self.thread=threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},daemon=True);self.thread.start()
        self.addCleanup(self.cleanup_server)
        self.base=f'/api/v1/sessions/{self.session.id}/streams/{self.stream.id}/instrument'

    def cleanup_server(self):
        self.server.shutdown();self.thread.join(3);self.workers.close();self.server.server_close()

    def request(self,method,path,payload=None,client_key=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
        data=None if payload is None else json.dumps(payload).encode()
        connection.request(method,path,body=data,headers={'Content-Type':'application/json','X-Instrument-Client':client_key or self.client_key})
        response=connection.getresponse();body=response.read();status=response.status;mime=response.getheader('Content-Type')
        connection.close()
        return status,(json.loads(body) if 'json' in (mime or '') else body),mime

    def submit(self,payload):
        status,result,_=self.request('POST',self.base+'/operations',payload)
        self.assertEqual(status,202,result)
        return result['data']['operation_id']

    def finish(self,operation):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            status,result,_=self.request('GET',self.base+'/operations/'+operation)
            self.assertEqual(status,200,result)
            if result['data']['state'] in {'succeeded','failed','cancelled','unknown'}:return result['data']
            time.sleep(.01)
        self.fail('Instrument job did not finish')

    def test_capabilities_and_explicit_lab_lease_over_real_http(self):
        status,result,_=self.request('GET',self.base+'/capabilities')
        self.assertEqual(status,200,result)
        self.assertTrue(result['data']['capabilities']['menu'])
        job=self.submit({'operation':'lease.acquire','parameters':{'acknowledge_rf':True},'request_id':'lease-1'})
        finished=self.finish(job);self.assertEqual(finished['state'],'succeeded',finished)
        token=finished['result']['lease_id']
        _,state,_=self.request('GET',self.base+'/settings')
        self.assertNotIn(token,str(state))
        self.assertEqual(state['data']['mode'],'lab')
        status,duplicate,_=self.request('POST',self.base+'/operations',{'operation':'lease.acquire','parameters':{'acknowledge_rf':True},'request_id':'lease-1'})
        self.assertEqual(status,202,duplicate);self.assertEqual(duplicate['data']['operation_id'],job)
        status,_,_=self.request('POST',self.base+'/operations',{'operation':'capture','request_id':'lease-1'})
        self.assertEqual(status,409)

    def test_completed_job_from_old_connection_is_explicitly_stale(self):
        job=self.submit({'operation':'inspect'})
        self.assertEqual(self.finish(job)['state'],'succeeded')
        self.workers.stop_stream(self.session.id,self.stream.id)
        self.workers.start_stream(self.session.id,self.stream.id)
        status,value,_=self.request('GET',self.base+'/operations/'+job)
        self.assertEqual(status,200,value)
        self.assertIs(value['data']['context_current'],False)

    def test_another_client_cannot_recover_lab_secret_from_job_history(self):
        job=self.submit({'operation':'lease.acquire','parameters':{'acknowledge_rf':True}})
        token=self.finish(job)['result']['lease_id']
        other=uuid.uuid4().hex
        status,history,_=self.request('GET',self.base+'/operations',client_key=other)
        self.assertEqual(status,200,history)
        self.assertNotIn(token,str(history))
        status,result,_=self.request('GET',self.base+'/operations/'+job,client_key=other)
        self.assertEqual(status,200,result)
        self.assertNotIn('lease_id',result['data']['result'])
        self.assertNotIn(token,str(result))

    def test_artifact_pages_are_scoped_and_do_not_hide_older_items(self):
        import base64
        for i in range(3):
            self.sessions.save_instrument_artifact(self.session.id,self.stream.id,dict(kind='sd_file',mime_type='application/octet-stream',filename=f'{i}.CSV',data_base64=base64.b64encode(bytes([i])).decode(),sha256=hashlib.sha256(bytes([i])).hexdigest(),metadata={'occurred_at':f'2026-11-09T00:00:0{i}Z'}))
        status,result,_=self.request('GET',self.base+'/artifacts?limit=1&offset=1')
        self.assertEqual(status,200,result)
        self.assertEqual([x['filename'] for x in result['data']['items']],['1.CSV'])

    def test_screen_preview_does_not_write_archive_and_saved_image_round_trips(self):
        lease=self.finish(self.submit({'operation':'lease.acquire','parameters':{'acknowledge_rf':True}}))
        self.assertEqual(lease['state'],'succeeded',lease)
        count=len(self.sessions.list_sweeps(self.session.id))
        preview=self.finish(self.submit({'operation':'capture','parameters':{'save':False}}))
        self.assertEqual(preview['state'],'succeeded',preview)
        self.assertIn('preview',preview['result'])
        status,items,_=self.request('GET',self.base+'/artifacts')
        self.assertEqual(status,200,items);self.assertEqual(items['data']['items'],[])
        self.assertEqual(len(self.sessions.list_sweeps(self.session.id)),count)
        saved=self.finish(self.submit({'operation':'capture','parameters':{'save':True}}))
        self.assertEqual(saved['state'],'succeeded',saved)
        item=saved['result']['artifact']
        status,image,mime=self.request('GET',self.base+'/artifacts/'+item['id']+'/content')
        self.assertEqual(status,200);self.assertIn('image/png',mime)
        self.assertEqual(hashlib.sha256(image).hexdigest(),item['sha256'])
        other=serial_session();other['name']='other';s=self.sessions.create_session(other)
        foreign=self.base.replace(self.session.id,s.id).replace(self.stream.id,s.streams[0].id)
        status,_,_=self.request('GET',foreign+'/artifacts/'+item['id']+'/content')
        self.assertEqual(status,404)
        reopened=SQLiteSessionStore(self.path)
        try:self.assertEqual(reopened.get_instrument_artifact(self.session.id,self.stream.id,item['id'])['payload'],image)
        finally:reopened.close()

    def test_unknown_raw_operation_and_mutation_without_lease_never_send_output(self):
        job=self.submit({'operation':'setting','parameters':{'name':'output_enabled','value':True}})
        result=self.finish(job);self.assertEqual(result['state'],'failed',result)
        self.assertNotIn('output on',self.wires[0].sent)
        status,result,_=self.request('POST',self.base+'/operations',{'operation':'raw','parameters':{'command':'output on'}})
        self.assertEqual(status,400,result)

    def test_foreign_stream_and_unknown_job_are_rejected(self):
        status,_,_=self.request('GET',self.base+'/operations/unknown')
        self.assertEqual(status,404)
        status,_,_=self.request('POST',self.base.replace(self.stream.id,'absent')+'/operations',{'operation':'capture'})
        self.assertEqual(status,404)
