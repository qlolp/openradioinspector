"""The offline modern client is served alongside all legacy routes."""
import http.client
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from radioinspector.web.server import create_server


class ModernWebAssetsTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory=Path(temporary.name)/'modern'
        assets=self.directory/'assets';assets.mkdir(parents=True)
        (self.directory/'index.html').write_text('<html><script src="/modern/assets/app.js"></script></html>')
        (assets/'app.js').write_text('globalThis.offline = true;')
        (Path(temporary.name)/'private.txt').write_text('private archive material')
        patcher=patch('radioinspector.web.server.MODERN_DIR',self.directory,create=True)
        patcher.start();self.addCleanup(patcher.stop)
        self.server=create_server(host='127.0.0.1',port=0)
        self.thread=threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},daemon=True);self.thread.start()
        self.addCleanup(self.thread.join);self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)

    def get(self,path):
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        try:
            connection.request('GET',path);response=connection.getresponse()
            return response.status,response.getheader('Content-Type'),response.read()
        finally:connection.close()

    def test_offline_assets_keep_legacy_and_api_reachable(self):
        self.assertEqual(self.get('/modern/')[0],200)
        status,mime,body=self.get('/modern/assets/app.js')
        self.assertEqual(status,200);self.assertTrue(mime.startswith('text/javascript'))
        self.assertEqual(body,b'globalThis.offline = true;')
        self.assertEqual(self.get('/')[0],200)
        self.assertEqual(self.get('/api/v1/health')[0],200)
        for path in ('/modern/../private.txt','/modern/%2e%2e/private.txt','/modern/assets/%2e%2e/%2e%2e/private.txt'):
            with self.subTest(path=path):
                status,_,body=self.get(path)
                self.assertEqual(status,404)
                self.assertNotIn(b'private archive material',body)

    def test_missing_build_is_explicit_and_keeps_legacy_available(self):
        (self.directory/'index.html').unlink()
        self.assertEqual(self.get('/modern/')[0],503)
        self.assertEqual(self.get('/')[0],200)
