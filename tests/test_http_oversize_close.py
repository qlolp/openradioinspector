"""Oversize rejection reaches the peer before a bounded, orderly close."""
from __future__ import annotations

import http.client
import io
import json
import queue
import socket
from socketserver import TCPServer
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from radioinspector.web import server


class _Input(io.BytesIO):
    def __init__(self, data, connection):
        super().__init__(data)
        self.connection = connection
        self.discard_requests = []
        self.discarded = 0
        self.unread_at_close = 0

    def read1(self, maximum=-1):
        self.discard_requests.append(maximum)
        # A peer that has not yet sent its upload must receive the rejection
        # before the server waits for additional input.
        if not self.connection.write_shutdown:
            raise AssertionError("body was drained before the response half-close")
        if not self.connection.sent.startswith(b"HTTP/1.0 413 "):
            raise AssertionError("body was drained before the 413 response")
        value = super().read1(maximum)
        self.discarded += len(value)
        return value

    def release(self, data):
        if self.closed:
            self.unread_at_close += len(data)
        else:
            position = self.tell()
            self.seek(0, io.SEEK_END)
            self.write(data)
            self.seek(position)

    def pending(self):
        return self.unread_at_close if self.closed else len(self.getvalue()) - self.tell()

    def close(self):
        if not self.closed:
            self.unread_at_close = self.pending()
        super().close()


class _UnreadAbortSocket:
    """Controlled Windows transport: close with unread input loses the reply.

    This models the observed Win10053 boundary, while the real HTTP tests use
    ordinary OS sockets. It does not simulate successful HTTP or application IO.
    """
    def __init__(self, request, *, late=b""):
        self.sent = bytearray()
        self.received = b""
        self.timeout = 7.0
        self.timeouts = []
        self.write_shutdown = False
        self.closed = False
        self.aborted = False
        self.late = late
        self.input = _Input(request, self)

    def makefile(self, mode, buffering=-1):
        assert mode == "rb"
        return self.input

    def sendall(self, data):
        self.sent.extend(data)

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeouts.append(value)
        self.timeout = value

    def shutdown(self, how):
        assert how == socket.SHUT_WR
        self.write_shutdown = True
        if self.late:
            self.input.release(self.late)
            self.late = b""

    def close(self):
        self.closed = True
        self.aborted = bool(self.input.pending())
        self.received = b"" if self.aborted else bytes(self.sent)


class _RejectOnlyHandler(server.Handler):
    dispatched = 0

    def _dispatch_post(self, route, payload):
        type(self).dispatched += 1
        raise AssertionError("a refused request reached application dispatch")


class OversizeCloseTransportTests(unittest.TestCase):
    def model(self, body=b"", *, declared=None, late=b"", headers=b"", path="/api/v1/sessions",
              content_type="application/json",
              handler_class=_RejectOnlyHandler):
        if declared is None:
            declared = str(server.MAX_CONTROL_REQUEST_BYTES + 1)
        wire = (f"POST {path} HTTP/1.0\r\nHost: localhost\r\n".encode()
                + f"Content-Type: {content_type}\r\nContent-Length: ".encode()
                + declared.encode() + b"\r\n" + headers + b"\r\n" + body)
        connection = _UnreadAbortSocket(wire, late=late)
        handler_class.dispatched = 0
        handler = handler_class(connection, ("127.0.0.1", 1234), SimpleNamespace())
        TCPServer.shutdown_request(SimpleNamespace(close_request=lambda item: item.close()), connection)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.input.closed)
        self.assertEqual(handler_class.dispatched, 0)
        return handler, connection

    def assert_rejection(self, connection):
        self.assertFalse(connection.aborted, "unread upload aborted the queued 413 response")
        self.assertTrue(connection.received.startswith(b"HTTP/1.0 413 "))
        self.assertEqual(connection.received.count(b"HTTP/1.0 "), 1)
        raw = connection.received.split(b"\r\n\r\n", 1)[1]
        self.assertEqual(json.loads(raw)["error"]["code"], "request_too_large")

    def test_full_limit_plus_one_reply_survives_unread_input_close(self):
        maximum = server.MAX_CONTROL_REQUEST_BYTES
        _, connection = self.model(b"x" * (maximum + 1))
        self.assert_rejection(connection)
        self.assertEqual(connection.input.discarded, maximum + 1)
        self.assertTrue(all(0 < size <= 65_536 for size in connection.input.discard_requests))
        self.assertEqual(connection.timeout, 7.0)

    def test_upload_arriving_after_response_half_close_is_drained_before_close(self):
        maximum = server.MAX_CONTROL_REQUEST_BYTES
        _, connection = self.model(b"x", late=b"x" * maximum)
        self.assert_rejection(connection)
        self.assertEqual(connection.input.discarded, maximum + 1)
        self.assertEqual(connection.timeout, 7.0)

    def test_huge_decimal_length_is_rejected_without_int_conversion_or_body_allocation(self):
        _, connection = self.model(declared="9" * 5000)
        self.assert_rejection(connection)
        self.assertEqual(connection.input.discarded, 0)
        self.assertTrue(all(size <= 65_536 for size in connection.input.discard_requests))

    def test_discard_byte_budget_does_not_follow_an_unbounded_declared_length(self):
        maximum = server.MAX_CONTROL_REQUEST_BYTES
        _, connection = self.model(b"x" * (maximum * 2), declared="9" * 5000)
        # The 413 is emitted before bounded cleanup. A peer that ignores the
        # response and exceeds its discard budget cannot keep the handler alive.
        self.assertTrue(connection.sent.startswith(b"HTTP/1.0 413 "))
        self.assertEqual(connection.input.discarded, maximum + 1)
        self.assertEqual(connection.timeout, 7.0)

    def test_discard_uses_one_absolute_deadline_and_restores_socket_timeout(self):
        maximum = server.MAX_CONTROL_REQUEST_BYTES
        with patch.object(server.time, "monotonic", side_effect=[0.0, 0.0, 1.0]):
            _, connection = self.model(b"x" * (maximum + 1))
        self.assertTrue(connection.sent.startswith(b"HTTP/1.0 413 "))
        self.assertEqual(connection.input.discarded, 65_536)
        self.assertEqual(connection.timeout, 7.0)
        self.assertLessEqual(connection.timeouts[0], server.REQUEST_BODY_TIMEOUT_SECONDS)

    def test_other_refusals_never_drain_a_body(self):
        for headers, status in (
            (b"Origin: https://foreign.invalid\r\n", 403),
            (b"Content-Length: 2\r\n", 400),
            (b"Transfer-Encoding: chunked\r\n", 400),
        ):
            with self.subTest(status=status):
                _, connection = self.model(b"x" * 100, headers=headers)
                self.assertTrue(connection.sent.startswith(f"HTTP/1.0 {status} ".encode()))
                self.assertEqual(connection.input.discard_requests, [])

    def test_content_type_and_pre_authorization_refusals_never_drain(self):
        _, connection = self.model(b"x" * 100, content_type="text/plain")
        self.assertTrue(connection.sent.startswith(b"HTTP/1.0 415 "))
        self.assertEqual(connection.input.discard_requests, [])

        def deny(*args):
            raise server.RuntimeBridgeError(401, "runtime_unauthorized")

        class Unauthorized(_RejectOnlyHandler):
            runtime_bridge = SimpleNamespace(authorize_control=deny)

        _, connection = self.model(b"x" * 100, path="/internal/runtime/restart",
                                   handler_class=Unauthorized)
        self.assertTrue(connection.sent.startswith(b"HTTP/1.0 401 "))
        self.assertEqual(connection.input.discard_requests, [])

    def test_finish_clears_its_request_flag_and_never_sends_twice(self):
        handler, connection = self.model(b"x" * (server.MAX_CONTROL_REQUEST_BYTES + 1))
        self.assert_rejection(connection)
        before = bytes(connection.sent), list(connection.input.discard_requests)
        handler.finish()
        self.assertEqual((bytes(connection.sent), connection.input.discard_requests), before)
        self.assertEqual(handler._rejected_body_budget, 0)


class _CountedReader:
    def __init__(self, reader):
        self.reader = reader
        self.requests = []
        self.bytes_read = 0

    def read1(self, size):
        self.requests.append(size)
        value = self.reader.read1(size)
        self.bytes_read += len(value)
        return value

    def __getattr__(self, name):
        return getattr(self.reader, name)


class OversizeCloseHTTPTests(unittest.TestCase):
    def setUp(self):
        self.http = server.create_server(host="127.0.0.1", port=0)
        self.closed = queue.Queue()
        self.finished = queue.Queue()
        self.request_threads = []
        handler_class = self.http.RequestHandlerClass
        original_setup = handler_class.setup
        original_finish = handler_class.finish

        def setup(handler):
            original_setup(handler)
            self.request_threads.append(threading.current_thread())
            handler._initial_timeout = handler.connection.gettimeout()
            handler.rfile = _CountedReader(handler.rfile)

        def finish(handler):
            try:
                original_finish(handler)
            finally:
                self.finished.put({
                    "initial_timeout": handler._initial_timeout,
                    "final_timeout": handler.connection.gettimeout(),
                    "input_closed": handler.rfile.closed,
                    "output_closed": handler.wfile.closed,
                    "read_requests": handler.rfile.requests,
                    "bytes_read": handler.rfile.bytes_read,
                })

        self.setup_patch = patch.object(handler_class, "setup", setup)
        self.finish_patch = patch.object(handler_class, "finish", finish)
        self.setup_patch.start()
        self.finish_patch.start()
        original_shutdown = self.http.shutdown_request

        def shutdown_request(connection):
            original_shutdown(connection)
            self.closed.put(connection.fileno() == -1)

        self.http.shutdown_request = shutdown_request
        self.listener = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.listener.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.http.shutdown()
        self.listener.join(timeout=3)
        for thread in self.request_threads:
            thread.join(timeout=3)
        self.http.server_close()
        self.finish_patch.stop()
        self.setup_patch.stop()
        self.assertFalse(self.listener.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in self.request_threads))

    def connect(self, declared):
        connection = socket.create_connection(self.http.server_address, timeout=3)
        connection.sendall(("POST /api/v1/sessions HTTP/1.1\r\nHost: localhost\r\n"
                            "Content-Type: application/json\r\n"
                            f"Content-Length: {declared}\r\n\r\n").encode())
        return connection

    def read_rejection(self, connection):
        response = http.client.HTTPResponse(connection)
        response.begin()
        self.assertEqual(response.status, 413)
        self.assertEqual(json.loads(response.read())["error"]["code"], "request_too_large")
        self.assertEqual(connection.recv(1), b"", "response must end before cleanup waits for input")

    def receipt(self, expected_bytes):
        self.assertTrue(self.closed.get(timeout=3), "the owned child socket was not closed")
        result = self.finished.get(timeout=3)
        self.assertTrue(result["input_closed"] and result["output_closed"])
        self.assertEqual(result["final_timeout"], result["initial_timeout"])
        self.assertEqual(result["bytes_read"], expected_bytes)
        self.assertTrue(all(0 < size <= 65_536 for size in result["read_requests"]))
        self.assertEqual(self.http.session_service.events(), [])
        self.assertEqual(self.http.session_service.list_sessions(), [])

    def test_complete_limit_plus_one_and_client_half_close_return_413(self):
        size = server.MAX_CONTROL_REQUEST_BYTES + 1
        for half_close in (False, True):
            with self.subTest(half_close=half_close), self.connect(size) as connection:
                connection.sendall(b"x" * size)
                if half_close:
                    connection.shutdown(socket.SHUT_WR)
                self.read_rejection(connection)
                self.receipt(size)

    def test_segmented_body_can_arrive_after_413_and_response_eof(self):
        size = server.MAX_CONTROL_REQUEST_BYTES + 1
        with self.connect(size) as connection:
            connection.sendall(b"x")
            self.read_rejection(connection)
            connection.sendall(b"x" * (size - 1))
            connection.shutdown(socket.SHUT_WR)
            self.receipt(size)

    def test_silent_oversize_and_huge_header_senders_cannot_hold_cleanup(self):
        for declared in (str(server.MAX_CONTROL_REQUEST_BYTES + 1), "9" * 5000):
            with self.subTest(digits=len(declared)), self.connect(declared) as connection:
                self.read_rejection(connection)
                # Keep the upload side open: the real absolute linger deadline,
                # rather than client EOF, must release the child socket.
                self.receipt(0)


if __name__ == "__main__":
    unittest.main()
