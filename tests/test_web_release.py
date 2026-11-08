"""Release checks for malformed public HTTP requests and resource limits."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

from radioinspector.panorama import PanoramaStore, ScanSettings
from radioinspector.trace import Trace
from radioinspector.web.server import ApplicationState, Handler


class TestWebRelease(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name).resolve() / 'input.pan'
        with PanoramaStore(self.path, ScanSettings(start_hz=1, stop_hz=3, points=3)) as store:
            store.append(Trace([1,2,3], [-90,-20,-90]))
        self.state = ApplicationState(database=str(self.path))
        handler = type('ReleaseHandler', (Handler,), {'state': self.state})
        self.server = ThreadingHTTPServer(('127.0.0.1',0),handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.state.close()
        self.temp.cleanup()

    def request(self, path, method='GET', body=None, headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            conn.request(method,path,body,headers or {})
            r=conn.getresponse(); return r.status,r.read()
        finally:conn.close()

    def test_invalid_queries_return_json_400(self):
        for path in ['/api/occupancy?threshold=no', '/api/occupancy?threshold=nan',
                     '/api/coverage?rows=101&cols=1', '/api/coverage?rows=-1',
                     '/api/measure?start=3&stop=1']:
            with self.subTest(path=path):
                code,data=self.request(path)
                self.assertEqual(code,400)
                self.assertIn('error',json.loads(data))

    def test_malformed_json_or_non_object_is_rejected(self):
        for body in [b'{',b'[]',b'\xff',b'{"offset_db":NaN}',
                     json.dumps({'offset_db':10**400}).encode(),
                     json.dumps({'frequencies':[1,2], 'levels':[10**400,0]}).encode()]:
            with self.subTest(body=body):
                code,_=self.request('/api/threshold','POST',body,{'Content-Type':'application/json'})
                self.assertEqual(code,400)
        self.assertTrue(self.state.store.load_threshold() is None)

    def test_oversized_body_is_rejected(self):
        code,_=self.request('/api/threshold','POST',b' '*65537,{'Content-Type':'application/json'})
        self.assertEqual(code,413)

    def test_cross_origin_or_simple_form_cannot_change_threshold(self):
        for headers,expected in [({'Content-Type':'text/plain'},415),
                                 ({'Content-Type':'application/json','Origin':'https://evil.invalid'},403)]:
            code,_=self.request('/api/threshold','POST',b'{"offset_db":1}',headers)
            self.assertEqual(code,expected)

    def test_loading_nonexistent_database_does_not_create_it(self):
        path=Path(self.temp.name)/'new'/ 'missing.pan'
        code,_=self.request('/api/database','POST',json.dumps({'path':str(path)}),{'Content-Type':'application/json'})
        self.assertEqual(code,400)
        self.assertFalse(path.exists())
        self.assertEqual(self.state.database,str(self.path))

    def test_valid_api_requests_still_work(self):
        for path in ['/','/api/state','/api/occupancy','/api/measure?start=1&stop=3']:
            code,data=self.request(path)
            self.assertEqual(code,200)
            self.assertTrue(data)
        code,data=self.request('/api/threshold','POST',b'{"offset_db":1}',{'Content-Type':'application/json'})
        self.assertEqual(code,200)
        self.assertTrue(json.loads(data)['ok'])
