"""Early refusals reach the peer without accepting any request-body bytes."""
from __future__ import annotations

import http.client
import io
import json
import queue
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from socketserver import TCPServer
from types import SimpleNamespace
from unittest.mock import patch

from radioinspector.web import server


class _UnreadInput(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.pending_at_close = 0
        self.body_reads = []

    def read1(self, size=-1):
        self.body_reads.append(size)
        raise AssertionError("an early refusal attempted to read the body")

    def release(self, data):
        if self.closed:
            self.pending_at_close += len(data)
        else:
            position = self.tell()
            self.seek(0, io.SEEK_END)
            self.write(data)
            self.seek(position)

    def pending(self):
        return self.pending_at_close if self.closed else len(self.getvalue()) - self.tell()

    def close(self):
        if not self.closed:
            self.pending_at_close = self.pending()
        super().close()


class _UnreadAbortSocket:
    """Unread close loses bytes not yet received by the controlled peer.

    The peer receives actual Handler output after FIN, with a short scheduling
    delay. This models the hosted reset boundary; real OS sockets are separate
    tests and hosted Windows remains necessary for platform acceptance.
    """
    def __init__(self, request, *, late=b""):
        self.input = _UnreadInput(request)
        self.sent = bytearray()
        self.received = b""
        self.late = late
        self.closed = False
        self.timeout = 7.0
        self.fin = threading.Event()
        self.peer_done = threading.Event()
        self.peer_delay = threading.Event()
        self.peer = threading.Thread(target=self._read_peer)
        self.peer.start()

    def _read_peer(self):
        if self.fin.wait(2):
            self.peer_delay.wait(0.02)
            if not self.closed:
                self.received = bytes(self.sent)
        self.peer_done.set()

    def makefile(self, mode, buffering=-1):
        assert mode == "rb"
        return self.input

    def sendall(self, data):
        self.sent.extend(data)

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def shutdown(self, how):
        assert how == socket.SHUT_WR
        if not self.fin.is_set():
            self.input.release(self.late)
            self.late = b""
            self.fin.set()

    def close(self):
        self.closed = True


class _RejectOnlyHandler(server.Handler):
    def _dispatch_post(self, route, payload):
        raise AssertionError("an early refusal reached application dispatch")


class EarlyRefusalTransportTests(unittest.TestCase):
    body = b'{"offset_db":1}'

    def model(self, *, content_type="application/json", extra=b"", late=False,
              length=None, path="/api/threshold", handler_class=_RejectOnlyHandler):
        if length is None:
            length = str(len(self.body))
        request = (f"POST {path} HTTP/1.0\r\nHost: localhost\r\n".encode()
                   + f"Content-Type: {content_type}\r\nContent-Length: {length}\r\n".encode()
                   + extra + b"\r\n" + (self.body[:1] if late else self.body))
        connection = _UnreadAbortSocket(request, late=self.body[1:] if late else b"")
        try:
            handler = handler_class(connection, ("127.0.0.1", 1234), SimpleNamespace())
            TCPServer.shutdown_request(SimpleNamespace(close_request=lambda item: item.close()), connection)
        finally:
            connection.peer.join(3)
        self.assertFalse(connection.peer.is_alive())
        self.assertTrue(connection.closed and connection.input.closed)
        self.assertEqual(connection.input.body_reads, [])
        self.assertEqual(connection.input.pending(), len(self.body))
        self.assertEqual(connection.timeout, 7.0)
        return handler, connection

    def assert_response(self, connection, status):
        self.assertTrue(connection.received.startswith(f"HTTP/1.0 {status} ".encode()),
                        "unread close discarded the refusal before the peer could read it")
        self.assertEqual(connection.received, bytes(connection.sent))
        self.assertEqual(connection.received.count(b"HTTP/1.0 "), 1)
        self.assertIn("error", json.loads(connection.received.split(b"\r\n\r\n", 1)[1]))

    def test_content_type_full_body_reaches_peer_without_reading(self):
        _, connection = self.model(content_type="text/plain")
        self.assert_response(connection, 415)

    def test_content_type_late_body_reaches_peer_without_reading(self):
        _, connection = self.model(content_type="text/plain", late=True)
        self.assert_response(connection, 415)

    def test_origin_full_body_reaches_peer_without_reading(self):
        _, connection = self.model(extra=b"Origin: https://evil.invalid\r\n")
        self.assert_response(connection, 403)

    def test_origin_late_body_reaches_peer_without_reading(self):
        _, connection = self.model(extra=b"Origin: https://evil.invalid\r\n", late=True)
        self.assert_response(connection, 403)

    def test_authentication_and_framing_refusals_remain_body_free(self):
        def deny(*args):
            raise server.RuntimeBridgeError(401, "runtime_unauthorized")

        class Unauthorized(_RejectOnlyHandler):
            runtime_bridge = SimpleNamespace(authorize_control=deny)

        _, connection = self.model(path="/internal/runtime/restart", handler_class=Unauthorized,
                                   late=True)
        self.assert_response(connection, 401)
        for extra, length in ((b"Content-Length: 15\r\n", "15"),
                              (b"Transfer-Encoding: chunked\r\n", "15"),
                              (b"", "-1")):
            with self.subTest(extra=extra, length=length):
                _, connection = self.model(extra=extra, length=length, late=True)
                self.assert_response(connection, 400)

    def test_pre_authorization_huge_decimal_length_never_converts_or_reads(self):
        _, connection = self.model(content_type="text/plain", length="9" * 5000)
        self.assert_response(connection, 415)

    def test_repeated_finish_does_not_start_a_second_grace_or_response(self):
        handler, connection = self.model(content_type="text/plain")
        self.assert_response(connection, 415)
        sent = bytes(connection.sent)
        with patch.object(server.threading, "Event", side_effect=AssertionError("second grace")):
            handler.finish()
        self.assertEqual(bytes(connection.sent), sent)
        self.assertEqual(connection.input.body_reads, [])

    def fast_request(self, wire, handler_class, *, readable=False):
        connection = _UnreadAbortSocket(wire)
        if readable:
            connection.input.read1 = lambda size: io.BytesIO.read1(connection.input, size)
        try:
            with patch.object(server.threading, "Event", side_effect=AssertionError("unexpected grace")):
                handler_class(connection, ("127.0.0.1", 1234), SimpleNamespace())
        finally:
            TCPServer.shutdown_request(SimpleNamespace(close_request=lambda item: item.close()), connection)
            connection.peer.join(3)
        self.assertFalse(connection.peer.is_alive())
        return bytes(connection.sent)

    def test_valid_body_success_and_parsed_errors_do_not_wait(self):
        class Accept(server.Handler):
            def _dispatch_post(self, route, payload):
                self._send_json({"ok": True}, 201)

        for body, status in ((b"{}", 201), (b"{", 400)):
            with self.subTest(status=status):
                wire = (b"POST /api/threshold HTTP/1.0\r\nHost: localhost\r\n"
                        b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                sent = self.fast_request(wire, Accept, readable=True)
                self.assertTrue(sent.startswith(f"HTTP/1.0 {status} ".encode()))

    def test_zero_length_refusal_get_and_early_success_do_not_wait(self):
        class ReadOnly(server.Handler):
            def do_GET(self):
                self._send_json({"ok": True})

        class EarlySuccess(server.Handler):
            def _runtime_request(self, method):
                self._send_json({"ok": True})
                return True

        for wire, handler_class, status in (
            (b"POST /api/threshold HTTP/1.0\r\nContent-Type: text/plain\r\n"
             b"Content-Length: 000\r\n\r\n", _RejectOnlyHandler, 415),
            (b"GET /api/state HTTP/1.0\r\n\r\n", ReadOnly, 200),
            (b"POST /internal/runtime/status HTTP/1.0\r\nContent-Length: 15\r\n\r\n"
             + self.body, EarlySuccess, 200),
        ):
            with self.subTest(status=status, handler=handler_class.__name__):
                sent = self.fast_request(wire, handler_class)
                self.assertTrue(sent.startswith(f"HTTP/1.0 {status} ".encode()))

    def test_grace_deadline_includes_half_close_and_is_capped_by_body_timeout(self):
        for body_timeout, shutdown_cost, expected_wait in ((10.0, 0.20, 0.05), (0.10, 0.04, 0.06)):
            with self.subTest(body_timeout=body_timeout):
                connection = _UnreadAbortSocket(
                    b"POST /api/threshold HTTP/1.0\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 15\r\n\r\n" + self.body)
                clock = [0.0]
                waits = []
                original_shutdown = connection.shutdown

                def shutdown(how):
                    clock[0] += shutdown_cost
                    original_shutdown(how)

                def wait(timeout):
                    waits.append(timeout)
                    self.assertTrue(connection.peer_done.wait(1))

                connection.shutdown = shutdown
                try:
                    with patch.object(server.time, "monotonic", side_effect=lambda: clock[0]), \
                         patch.object(server, "REQUEST_BODY_TIMEOUT_SECONDS", body_timeout), \
                         patch.object(server.threading, "Event", return_value=SimpleNamespace(wait=wait)):
                        _RejectOnlyHandler(connection, ("127.0.0.1", 1234), SimpleNamespace())
                finally:
                    TCPServer.shutdown_request(SimpleNamespace(close_request=lambda item: item.close()), connection)
                    connection.peer.join(3)
                self.assertEqual(len(waits), 1)
                self.assertAlmostEqual(waits[0], expected_wait)
                self.assertEqual(connection.input.body_reads, [])
                self.assert_response(connection, 415)


class _NoBodyReader:
    def __init__(self, reader):
        self.reader = reader
        self.body_reads = []

    def read1(self, size):
        self.body_reads.append(size)
        raise AssertionError("an early refusal attempted a socket body read")

    def __getattr__(self, name):
        return getattr(self.reader, name)


class EarlyRefusalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.finished = queue.Queue()
        self.closed = queue.Queue()
        self.request_threads = []
        owner = self

        class TracedHandler(_RejectOnlyHandler):
            def setup(self):
                super().setup()
                self.rfile = _NoBodyReader(self.rfile)
                self._initial_timeout = self.connection.gettimeout()
                owner.request_threads.append(threading.current_thread())

            def finish(self):
                try:
                    super().finish()
                finally:
                    owner.finished.put({"body_reads": self.rfile.body_reads,
                                        "input_closed": self.rfile.closed,
                                        "output_closed": self.wfile.closed,
                                        "initial_timeout": self._initial_timeout,
                                        "final_timeout": self.connection.gettimeout()})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), TracedHandler)
        original_shutdown = self.http.shutdown_request

        def shutdown(connection):
            original_shutdown(connection)
            self.closed.put(connection.fileno() == -1)

        self.http.shutdown_request = shutdown
        self.listener = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.01))
        self.listener.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.http.shutdown()
        self.listener.join(3)
        self.http.server_close()
        for worker in self.request_threads:
            worker.join(3)
        self.assertFalse(self.listener.is_alive())
        self.assertTrue(all(not worker.is_alive() for worker in self.request_threads))

    def check_request(self, *, origin=False, late=False, body=True):
        payload = EarlyRefusalTransportTests.body
        extra = (b"Content-Type: application/json\r\nOrigin: https://evil.invalid\r\n"
                 if origin else b"Content-Type: text/plain\r\n")
        expected = 403 if origin else 415
        with socket.create_connection(self.http.server_address, timeout=3) as connection:
            connection.sendall(b"POST /api/threshold HTTP/1.0\r\nHost: localhost\r\n"
                               + extra + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                               + ((payload[:1] if late else payload) if body else b""))
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, expected)
            self.assertIn("error", json.loads(response.read()))
            self.assertEqual(connection.recv(1), b"", "FIN must precede the no-read grace")
            if late:
                connection.sendall(payload[1:])
            self.assertTrue(self.closed.get(timeout=3))
            receipt = self.finished.get(timeout=3)
        self.assertEqual(receipt["body_reads"], [])
        self.assertTrue(receipt["input_closed"] and receipt["output_closed"])
        self.assertEqual(receipt["initial_timeout"], receipt["final_timeout"])

    def test_complete_body_refusals_reach_the_real_peer(self):
        for origin in (False, True):
            with self.subTest(origin=origin):
                self.check_request(origin=origin)

    def test_body_can_arrive_after_refusal_and_response_fin(self):
        for origin in (False, True):
            with self.subTest(origin=origin):
                self.check_request(origin=origin, late=True)

    def test_silent_upload_cannot_hold_the_owned_request(self):
        self.check_request(body=False)


if __name__ == "__main__":
    unittest.main()
