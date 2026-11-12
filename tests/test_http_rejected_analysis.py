"""Real TCP rejection ordering; synthetic archives only, no device I/O."""

from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import select
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from radioinspector.web import server
from tests import test_spectral_mask as masks
from tests.test_platform import session_payload
from tests.test_sweep_comparison import create_session, measurement


QUERY_MESSAGE = "Анализ не принимает параметры запроса"
# Public endpoint budgets, including larger artifact-based requests. An error
# path must not silently substitute the ordinary 64 KiB control-body budget.
ANALYSIS_BUDGETS = (
    ("sweep-mask-analysis", 524_288),
    ("sweep-average-comparison", 8_192),
    ("sweep-three-thresholds", 8_192),
    ("sweep-average-change-detection", 8_192),
    ("sweep-context", 8_192),
    ("sweep-occupancy", 8_192),
    ("candidate-frequency-rounding", 524_288),
    ("frequency-rounding", 524_288),
    ("frequency-counter", 8_192),
    ("dvb-evaluation", 8_192),
    ("measurement-map", 262_144),
    ("sweep-quality", 8_192),
)


class RejectedAnalysisHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "archive.sqlite"
        self.server = server.create_server(host="127.0.0.1", port=0, session_database=str(self.path))
        self.service = self.server.session_service
        self.session = create_session(self.service)
        self.service.ingest_sweep(measurement(self.session.id, self.session.streams[0].id))
        self.root = f"/api/v1/sessions/{self.session.id}/"
        self.route = self.root + "sweep-mask-analysis"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive(), "owned HTTP listener did not stop")
        self.server.server_close()

    def snapshot(self):
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as connection:
            digest = hashlib.sha256("\n".join(connection.iterdump()).encode()).hexdigest()
        return copy.deepcopy((self.service.health(), self.service.events(),
                              self.service.archive_capacity(), self.service.list_sweeps(self.session.id),
                              self.service.list_sweep_references(self.session.id), digest))

    def raw(self, headers, *, path=None, body=b"", shutdown=False):
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall((f"POST {path or self.route + '?x=1'} HTTP/1.1\r\n"
                                "Host: localhost\r\n" + headers + "\r\n").encode() + body)
            if shutdown:
                connection.shutdown(socket.SHUT_WR)
            return self.read_response(connection)

    def read_response(self, connection):
        response = http.client.HTTPResponse(connection)
        response.begin()
        result = response.status, json.loads(response.read())
        self.assertEqual(connection.recv(1), b"", "rejected request connection was reused")
        return result

    def post(self, path, body):
        return self.raw(f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n",
                        path=path, body=body)

    def test_snapshot_closes_real_read_handle_on_success_and_hash_error(self):
        connect = sqlite3.connect
        for hash_error in (False, True):
            opened = []

            def tracked_connect(*args, **kwargs):
                connection = connect(*args, **kwargs)
                opened.append(connection)
                return connection

            with self.subTest(hash_error=hash_error):
                try:
                    with patch.object(sqlite3, "connect", tracked_connect):
                        if hash_error:
                            with patch.object(hashlib, "sha256", side_effect=ValueError("controlled hash failure")):
                                with self.assertRaises(ValueError):
                                    self.snapshot()
                        else:
                            self.snapshot()
                    self.assertEqual(len(opened), 1)
                    with self.assertRaises(sqlite3.ProgrammingError):
                        opened[0].execute("SELECT 1")
                finally:
                    for connection in opened:
                        connection.close()

    def test_segmented_query_body_finishes_before_error_and_orderly_close(self):
        before = self.snapshot()
        body = json.dumps({"sweep_id": "sweep-0", "mask_json": masks.document(name="Cafe\u0301 🌈")},
                          ensure_ascii=False).encode("utf-8")
        for suffix in ("?", "?x=1", "?x=", "?&", "?sweep_id=sweep-0"):
            entered = threading.Event()
            original = self.server.RequestHandlerClass._post

            def observed(handler):
                entered.set()
                return original(handler)

            with self.subTest(suffix=suffix), patch.object(self.server.RequestHandlerClass, "_post", observed):
                with socket.create_connection(self.server.server_address, timeout=3) as connection:
                    connection.sendall((f"POST {self.route + suffix} HTTP/1.1\r\nHost: localhost\r\n"
                                        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode())
                    self.assertTrue(entered.wait(2), "request headers were not dispatched")
                    if select.select([connection], [], [], 0.1)[0]:
                        status, _ = self.read_response(connection)
                        self.fail(f"HTTP {status} closed before the declared POST body arrived")
                    connection.sendall(body[:1])
                    if select.select([connection], [], [], 0.05)[0]:
                        status, _ = self.read_response(connection)
                        self.fail(f"HTTP {status} closed with a partial POST body unread")
                    connection.sendall(body[1:])
                    status, response = self.read_response(connection)
                    self.assertEqual((status, response["error"]["code"], response["error"]["message"]),
                                     (400, "invalid_request", QUERY_MESSAGE))
        self.assertEqual(self.snapshot(), before)

    def test_each_query_route_uses_its_own_body_budget(self):
        before = self.snapshot()
        for resource, maximum in ANALYSIS_BUDGETS:
            with self.subTest(resource=resource):
                path = self.root + resource + "?x=1"
                status, response = self.raw(f"Content-Type: application/json\r\nContent-Length: {maximum + 1}\r\n",
                                            path=path)
                self.assertEqual((status, response["error"]["code"]), (413, "request_too_large"))
                # The query refusal has priority over JSON parsing, even at the
                # exact route limit; this is not a valid JSON document.
                status, response = self.post(path, b"x" * maximum)
                self.assertEqual((status, response["error"]["message"]), (400, QUERY_MESSAGE))
        self.assertEqual(self.snapshot(), before)

    def test_query_framing_refusals_do_not_wait_for_a_body(self):
        before = self.snapshot()
        for framing, expected in (
            ("", 400), ("Content-Length: -1\r\n", 400),
            ("Content-Length: +1\r\n", 400), ("Content-Length: 2, 2\r\n", 400),
            ("Content-Length: 2\r\nContent-Length: 2\r\n", 400),
            ("Transfer-Encoding: chunked\r\n", 400),
            ("Content-Length: 2\r\nTransfer-Encoding: identity\r\n", 400),
            ("Content-Length: " + "9" * 5000 + "\r\n", 413),
        ):
            with self.subTest(framing=framing[:80]):
                status, response = self.raw("Content-Type: application/json\r\n" + framing)
                self.assertEqual(status, expected)
                self.assertEqual(response["error"]["code"], "request_too_large" if expected == 413 else "invalid_request")
        self.assertEqual(self.snapshot(), before)

    def test_query_keeps_priority_over_invalid_json_and_utf8(self):
        before = self.snapshot()
        for body in (b"", b"{", b"[]", b"\xff", b'{"x":1,"x":2}', b'{"x":NaN}', b"[" * 1200):
            with self.subTest(body=body[:40]):
                status, response = self.post(self.route + "?", body)
                self.assertEqual((status, response["error"]["message"]), (400, QUERY_MESSAGE))
        self.assertEqual(self.snapshot(), before)

    def test_query_incomplete_body_and_deadline_close_without_mutation(self):
        before = self.snapshot()
        headers = "Content-Type: application/json\r\nContent-Length: 20\r\n"
        status, response = self.raw(headers, body=b"{", shutdown=True)
        self.assertEqual((status, response["error"]["message"]), (400, "Неполное тело запроса"))
        with patch.object(server, "REQUEST_BODY_TIMEOUT_SECONDS", 0.1):
            status, response = self.raw(headers, body=b"{")
        self.assertEqual((status, response["error"]["code"]), (408, "request_timeout"))
        self.assertEqual(self.snapshot(), before)

    def test_content_type_and_origin_still_reject_before_reading(self):
        before = self.snapshot()
        for headers, expected in (
            ("Content-Type: text/plain\r\nContent-Length: 20\r\n", 415),
            ("Content-Type: application/json\r\nOrigin: https://foreign.invalid\r\nContent-Length: 20\r\n", 403),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(self.raw(headers)[0], expected)
        self.assertEqual(self.snapshot(), before)

    def test_valid_json_routes_preserve_ascii_utf8_metadata_and_strict_parse(self):
        payload = session_payload()
        payload["name"] = "ASCII / Cafe\u0301 🌈"
        payload["metadata"] = {"ASCII": "plain", "é": "NFC", "e\u0301": "NFD", "µ": "µs"}
        status, response = self.post("/api/v1/sessions", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(status, 201)
        self.assertEqual(response["data"]["name"], payload["name"])
        self.assertEqual(response["data"]["metadata"], payload["metadata"])
        before = self.snapshot()
        for body in (b"{", b"[]", b"\xff", b'{"name":"a","name":"b"}', b'{"name":"a","x":NaN}'):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/v1/sessions", body)[0], 400)
        body = json.dumps({"sweep_id": "sweep-0", "mask_json": masks.document()}).encode()
        status, response = self.post(self.route, body)
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["sweep"]["levels"], [-80, -70, -60, -50])
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
