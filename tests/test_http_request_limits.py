"""Request rejection precedes mutation; no hardware or large allocations."""
import http.client
import io
import json
import socket
import threading
import unittest
from email.message import Message
from unittest.mock import Mock, patch

from radioinspector.web import server


class RequestLimitsTests(unittest.TestCase):
    def setUp(self):
        self.server = server.create_server(host='127.0.0.1', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, body, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('POST', '/api/v1/sessions', body,
                               headers or {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw(self, headers, body=b'', shutdown=False):
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3) as connection:
            connection.sendall(b'POST /api/v1/sessions HTTP/1.0\r\nHost: localhost\r\n'
                               b'Content-Type: application/json\r\n' + headers + b'\r\n' + body)
            if shutdown:
                connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, json.loads(response.read())

    def test_exact_control_limit_succeeds_and_next_byte_is_413(self):
        body = b'{"name":"boundary"}'
        maximum = server.MAX_CONTROL_REQUEST_BYTES
        self.assertEqual(self.request(body + b' ' * (maximum - len(body)))[0], 201)
        before = self.server.session_service.events()
        self.assertEqual(self.request(body + b' ' * (maximum + 1 - len(body)))[0], 413)
        self.assertEqual(self.server.session_service.events(), before)
        self.assertEqual(len(self.server.session_service.list_sessions()), 1)

    def test_framing_and_incomplete_bodies_are_rejected_without_mutation(self):
        cases = [(b'Content-Length: 999999999999999999999999\r\n', b'', False, 413),
                 (b'Content-Length: ' + b'9' * 5000 + b'\r\n', b'', False, 413),
                 (b'Content-Length: 2\r\nContent-Length: 2\r\n', b'{}', False, 400),
                 (b'Transfer-Encoding: chunked\r\n', b'', False, 400),
                 (b'Content-Length: -1\r\n', b'', False, 400),
                 (b'Content-Length: 20\r\n', b'{}', True, 400)]
        for headers, body, shutdown, status in cases:
            with self.subTest(headers=headers[:80]):
                self.assertEqual(self.raw(headers, body, shutdown)[0], status)
        self.assertEqual(self.server.session_service.list_sessions(), [])
        self.assertEqual(self.server.session_service.events(), [])

    def test_invalid_json_never_creates_session(self):
        for body in (b'{', b'[]', b'\xff', b'{"name":"a","name":"b"}',
                     b'{"name":"a","x":NaN}', b'{"name":"a","x":1e999}'):
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(self.server.session_service.events(), [])

    def test_incomplete_sender_times_out(self):
        with patch.object(server, 'REQUEST_BODY_TIMEOUT_SECONDS', 0.1):
            self.assertEqual(self.raw(b'Content-Length: 20\r\n', b'{')[0], 408)
        self.assertEqual(self.server.session_service.events(), [])

    def test_absolute_deadline_and_socket_timeout_restoration(self):
        handler = server.Handler.__new__(server.Handler)
        handler.headers = Message()
        handler.headers['Content-Length'] = '2'
        handler.connection = Mock()
        handler.connection.gettimeout.return_value = None
        handler.rfile = io.BytesIO(b'{}')
        with patch.object(server.time, 'monotonic', side_effect=[0, 0, 11]):
            with self.assertRaises(server.RequestError) as caught:
                handler._read_json()
        self.assertEqual(caught.exception.status, 408)
        self.assertEqual(handler.connection.settimeout.call_args.args, (None,))

    def test_instrument_empty_body_is_rejected_before_dispatch(self):
        for resource in ('operations', 'control-lease', 'operations/job/cancel'):
            handler = server.Handler.__new__(server.Handler)
            handler.path = '/api/v1/sessions/session/streams/stream/instrument/' + resource
            handler.headers = Message()
            handler.headers['Content-Type'] = 'application/json'
            handler.headers['Content-Length'] = '0'
            handler.connection = Mock()
            handler.connection.gettimeout.return_value = None
            handler.rfile = io.BytesIO()
            handler._api_v1_post = Mock()
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                handler._post()
            handler._api_v1_post.assert_not_called()

    def test_data_routes_use_measurement_budget(self):
        session = self.server.session_service.create_session({'name': 'data'})
        # Shape validation may fail, but the control-body budget must not reject it.
        for suffix in ('sweeps', 'observations', 'sweeps/batch', 'phonogram/frames'):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
            try:
                body = b'{}' + b' ' * server.MAX_CONTROL_REQUEST_BYTES
                connection.request('POST', f'/api/v1/sessions/{session.id}/{suffix}', body,
                                   {'Content-Type': 'application/json'})
                response = connection.getresponse()
                response.read()
                self.assertNotEqual(response.status, 413)
            finally:
                connection.close()
