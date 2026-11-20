"""Loopback producer socket regressions using the ordinary ingest and verification routes."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from urllib.request import HTTPHandler, Request

from benchmarks.ori_bench.client import Client, TransportFailure
from benchmarks.ori_bench.config import Config
from benchmarks.ori_bench.producer_http import ProducerHTTPHandler, RequestSocket
from benchmarks.ori_bench.runner import run
from benchmarks.ori_bench.source import identifier
from benchmarks.tests.test_benchmarks import http_fixture


@contextmanager
def serving(handler):
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield 'http://127.0.0.1:' + str(server.server_port)
        finally:
            server.shutdown()
            thread.join()


class ProducerConnectTests(unittest.TestCase):
    def test_stalled_connect_discards_original_socket_then_posts_exact_offers_once(self):
        # Make the peer accept the first real socket but withhold its connect
        # completion from http.client until the socket timeout. No HTTP bytes
        # can be written while HTTPConnection.connect is still on the stack.
        connect = socket.socket.connect
        attempts = []

        def delayed_first(sock, address):
            if not threading.current_thread().name.startswith('benchmark-producer-'):
                return connect(sock, address)
            if attempts:
                self.assertEqual(attempts[-1].fileno(), -1)
            attempts.append(sock)
            connect(sock, address)
            if len(attempts) == 1:
                self.assertRaises(TimeoutError, sock.recv, 1)
                raise TimeoutError('withheld handshake completion')

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with http_fixture(root / 'peer') as (peer, url):
                config = Config(url, root / 'results', run_id='connect-originals', workers=1,
                                duration_seconds=.2, audio_segment_seconds=.1, request_timeout=1.5,
                                points=4096, history_repeats=1)
                with patch.object(socket.socket, 'connect', delayed_first):
                    report, status = run(config)
                expected_ids = {identifier(config, kind, i)
                                for kind, count in config.planned_counts().items() for i in range(count)}
                stored_ids = {row[0] for row in peer.db.execute(
                    'SELECT id FROM measurements UNION ALL SELECT id FROM artifacts')}
                self.assertEqual(stored_ids, expected_ids)
                path = '/api/v1/sessions/' + config.session_id
                for route, count in (('/sweeps/batch', 2), ('/observations/batch', 2), ('/audio-segments', 2)):
                    self.assertEqual(peer.posts.count(path + route), count)
        self.assertEqual(status, 0, report['errors'])
        self.assertTrue(all(sock.fileno() == -1 for sock in attempts))
        self.assertEqual(len(attempts), len(expected_ids) + 1)
        self.assertEqual(report['checks']['archive_integrity']['status'], 'passed')
        self.assertEqual(report['checks']['complete_delivery']['status'], 'passed')
        self.assertTrue(report['audio']['all_offered_pcm_preserved'])
        tcp = report['producer_loopback_tcp']
        self.assertEqual(tcp['requests'], len(expected_ids))
        self.assertEqual(tcp['attempts'], len(attempts))
        self.assertEqual(tcp['connect_timeouts'], 1)
        sample, = tcp['samples']
        self.assertIn(sample['id'], expected_ids)
        self.assertTrue(sample['retained_source_verified'])
        self.assertEqual([item['outcome'] for item in sample['attempts']], ['timeout', 'connected'])
        self.assertTrue(all(item['elapsed_ms'] >= 0 for item in sample['attempts']))

    def test_os_connect_timeout_can_recover_on_the_same_listener(self):
        # Hold this disposable listener's accept queue full until its first
        # real connect(2) times out, then resume accepting on that SAME port.
        class SmallQueue(ThreadingHTTPServer):
            request_queue_size = 1

        received = []
        class Peer(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(201)
                self.send_header('Content-Length', '13')
                self.end_headers()
                self.wfile.write(b'{"data":"ok"}')

            def log_message(self, *args):
                pass

        with SmallQueue(('127.0.0.1', 0), Peer) as server, ExitStack() as fillers:
            for _ in range(16):
                sock = fillers.enter_context(socket.socket())
                sock.settimeout(.05)
                try:
                    sock.connect(server.server_address)
                except TimeoutError:
                    break
                except ConnectionRefusedError:
                    self.skipTest('The OS refuses connections to a full queue immediately')
            else:
                self.skipTest('The OS enlarged the local listener queue')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            connect = socket.socket.connect
            attempts = []

            def resume_after_timeout(sock, endpoint):
                self.assertEqual(endpoint, server.server_address)
                if attempts:
                    self.assertEqual(attempts[-1].fileno(), -1)
                attempts.append(sock)
                try:
                    return connect(sock, endpoint)
                except TimeoutError:
                    if not thread.is_alive():
                        thread.start()
                    raise

            client = Client(Config('http://127.0.0.1:' + str(server.server_port), Path('.'),
                                   request_timeout=3), producer=True)
            try:
                with patch.object(socket.socket, 'connect', resume_after_timeout):
                    value = client.json('/api/v1/sessions', method='POST', payload={'id': 'original'}, status=201)
            finally:
                if thread.is_alive():
                    server.shutdown()
                    thread.join()
            self.assertEqual(value, 'ok')
            self.assertEqual(received, [('/api/v1/sessions', b'{"id":"original"}')])
            self.assertEqual(client.connect_attempts[0]['outcome'], 'timeout')
            self.assertEqual(client.connect_attempts[-1]['outcome'], 'connected')
            self.assertTrue(all(sock.fileno() == -1 for sock in attempts))

    def test_repeated_connect_timeouts_share_one_budget_and_send_nothing(self):
        connect = socket.socket.connect
        sockets = []
        now = 10.
        def timeout_before_write(sock, endpoint):
            nonlocal now
            if sockets:
                self.assertEqual(sockets[-1].fileno(), -1)
            sockets.append(sock)
            connect(sock, endpoint)
            now += sock.gettimeout()
            raise TimeoutError('withheld handshake completion')

        with tempfile.TemporaryDirectory() as folder, http_fixture(Path(folder) / 'peer') as (peer, origin):
            client = Client(Config(origin, Path(folder), request_timeout=2), producer=True)
            clock = SimpleNamespace(monotonic=lambda: now)
            with patch.object(socket.socket, 'connect', timeout_before_write), \
                    patch('benchmarks.ori_bench.producer_http.time', clock), self.assertRaises(TransportFailure) as caught:
                client.json('/api/v1/sessions', method='POST', payload={'id': 'original'}, status=201)
            self.assertEqual(peer.posts, [])
        self.assertTrue(all(sock.fileno() == -1 for sock in sockets))
        self.assertEqual(caught.exception.details['phase'], 'tcp_connect')
        self.assertFalse(caught.exception.details['request_write_completed'])
        self.assertEqual([item['outcome'] for item in client.connect_attempts], ['timeout'] * 4)
        self.assertEqual([item['timeout_ms'] for item in client.connect_attempts], [250., 500., 1000., 250.])
        self.assertEqual(sum(item['elapsed_ms'] for item in client.connect_attempts), 2000.)

    def test_body_uses_the_time_left_after_connect_and_headers_without_replay(self):
        received = []
        class Peer(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(201)
                self.send_header('Content-Length', '13')
                self.end_headers()
                # Each byte arrives within the request timeout. The complete
                # acknowledgement takes longer than the original deadline.
                for byte in b'{"data":"ok"}':
                    try:
                        self.wfile.write(bytes([byte]))
                    except OSError:
                        return
                    time.sleep(.05)

            def log_message(self, *args):
                pass

        with serving(Peer) as origin:
            client = Client(Config(origin, Path('.'), request_timeout=.4), producer=True)
            with self.assertRaises(TransportFailure) as caught:
                client.json('/api/v1/sessions', method='POST', payload={'id': 'original'}, status=201)
        self.assertEqual(received, [b'{"id":"original"}'])
        self.assertEqual(caught.exception.details['phase'], 'response_body')
        self.assertEqual(caught.exception.details['response_status'], 201)
        self.assertTrue(caught.exception.details['request_write_completed'])
        self.assertEqual([attempt['outcome'] for attempt in client.connect_attempts], ['connected'])

    def test_partial_http_write_is_unknown_and_is_never_retried(self):
        received = []
        finished = threading.Event()
        class Peer(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                finished.set()

            def log_message(self, *args):
                pass

        body = b'{"id":"original"}'
        sendall = RequestSocket.sendall

        def partial_write(sock, data, flags=0):
            if body in data:
                sendall(sock, data[:data.index(body) + 5], flags)
                raise TimeoutError('after partial body write')
            return sendall(sock, data, flags)

        with serving(Peer) as origin:
            client = Client(Config(origin, Path('.')), producer=True)
            with patch.object(RequestSocket, 'sendall', partial_write), self.assertRaises(TransportFailure) as caught:
                client.json('/api/v1/sessions', method='POST', payload={'id': 'original'}, status=201)
            self.assertTrue(finished.wait(3))
        self.assertEqual(received, [body[:5]])
        self.assertEqual(caught.exception.details['phase'], 'request_send')
        self.assertFalse(caught.exception.details['request_write_completed'])
        self.assertEqual([attempt['outcome'] for attempt in client.connect_attempts], ['connected'])

    def test_remote_name_proxy_and_get_keep_the_standard_handler(self):
        handler = ProducerHTTPHandler([])
        proxied = Request('http://127.0.0.1/api/v1/test', method='POST')
        proxied.set_proxy('127.0.0.1:9999', 'http')
        for request in (proxied, Request('http://localhost/api/v1/test', method='POST'),
                        Request('http://192.0.2.1/api/v1/test', method='POST'),
                        Request('http://127.0.0.1/api/v1/test')):
            with self.subTest(url=request.full_url), patch.object(HTTPHandler, 'http_open') as standard:
                handler.http_open(request)
                standard.assert_called_once_with(request)
        self.assertEqual(handler.attempts, [])


if __name__ == '__main__':
    unittest.main()
