"""Bounded synthetic HTTP checks; no production CLI or external services."""
import http.client
import threading
import unittest

from tests.spectrum_view_smoke_server import assert_no_workers, create_fixture


class SpectrumFixtureTests(unittest.TestCase):
    def test_reads_and_fail_closed_mutations(self):
        server, _service = create_fixture()
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for method, path, expected in [
                ('GET', '/platform', 200), ('GET', '/api/v1/sessions', 200),
                ('GET', '/api/v1/sessions/view-one', 200),
                ('GET', '/api/v1/events', 403),
                ('POST', '/api/v1/sessions', 403),
                ('POST', '/api/database', 403),
                ('PUT', '/api/v1/sessions/view-one', 403),
                ('DELETE', '/api/v1/sessions/view-one', 403),
                ('PATCH', '/anything', 403), ('HEAD', '/platform', 403),
                ('OPTIONS', '/', 403), ('CONNECT', '/', 403),
            ]:
                with self.subTest(method=method, path=path):
                    client = http.client.HTTPConnection(*server.server_address, timeout=3)
                    try:
                        client.request(method, path)
                        response = client.getresponse()
                        self.assertEqual(response.status, expected)
                        response.read()
                        assert_no_workers(server)
                    finally:
                        client.close()
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()

    def test_complete_platform_refresh_reads(self):
        import json
        server, _service = create_fixture()
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def get(path):
            client = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                client.request('GET', '/api/v1' + path)
                response = client.getresponse()
                self.assertEqual(response.status, 200, path)
                payload = json.loads(response.read())
                assert_no_workers(server)
                return payload['data']
            finally:
                client.close()

        try:
            # Exact read set used by platform.js performRefresh(), for both scopes.
            health = get('/health')
            sessions = get('/sessions')
            self.assertEqual(len(sessions), 2)
            for session in sessions:
                prefix = '/sessions/' + session['id']
                stream = session['streams'][0]['id']
                for endpoint in ('workers', 'candidates', 'investigations',
                                 'investigations/next', 'automatic-handoff',
                                 'automatic-detection'):
                    get(prefix + '/' + endpoint)
                get(f"/events/history?after={max(0, health['last_event_id'] - 200)}&limit=200")
                sweeps = get(prefix + f'/sweeps?stream_id={stream}&limit=40')
                self.assertEqual(len(sweeps), 4)
                self.assertTrue(all(sweep['stream_id'] == stream for sweep in sweeps))
                self.assertTrue(all(sweep['session_id'] == session['id'] for sweep in sweeps))
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()

    def test_private_advance_and_eof_shutdown(self):
        import json
        import subprocess
        import sys
        process = subprocess.Popen(
            [sys.executable, '-m', 'tests.spectrum_view_smoke_server'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            output, errors = process.communicate('advance\nadvance\n', timeout=10)
            self.assertEqual(process.returncode, 0, errors)
            records = [json.loads(line) for line in output.splitlines()]
            self.assertTrue(records[0]['ready'])
            self.assertEqual(records[1:], [{'advanced': 5}, {'advanced': 6}])
            self.assertEqual(records[0]['scopes'],
                             ['view-one/spectrum-one', 'view-two/spectrum-two'])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_accepted_half_open_request_has_bounded_cleanup(self):
        import socket
        server, _service = create_fixture()
        accepted = threading.Event()
        closed = threading.Event()
        original = server.RequestHandlerClass

        class ObservedHandler(original):
            def setup(self):
                super().setup()
                accepted.set()

        server.RequestHandlerClass = ObservedHandler
        serving = threading.Thread(target=server.serve_forever)
        serving.start()
        connection = None
        closing = None
        try:
            connection = socket.create_connection(server.server_address, timeout=3)
            connection.sendall(b'GET /platform HTTP/1.1\r\nHost: localhost\r\n')
            self.assertTrue(accepted.wait(3), 'Server did not accept the test socket')

            def close_server():
                server.shutdown()
                server.server_close()
                closed.set()

            closing = threading.Thread(target=close_server)
            closing.start()
            self.assertTrue(closed.wait(3), 'Accepted half-open socket blocked cleanup')
        finally:
            if connection is not None:
                connection.close()
            if closing is None:
                server.shutdown()
                server.server_close()
            else:
                closing.join(3)
            serving.join(3)
