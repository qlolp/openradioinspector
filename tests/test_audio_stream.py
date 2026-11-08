"""Network delivery of demodulated audio: bounded fan-out, counted drops."""

from __future__ import annotations

import http.client
import json
import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from radioinspector.audio_stream import (
    MAX_QUEUED_BLOCKS, MAX_SUBSCRIBERS, AudioStreamError, AudioStreamHub, pcm16_bytes,
)
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session

RATE = 48_000


class AudioStreamHubTests(unittest.TestCase):
    def test_pcm16_encoding_matches_wav(self):
        data = pcm16_bytes(np.array([-1.0, 0.0, 1.0]))
        self.assertEqual(len(data), 6)
        self.assertEqual(data[:2], b"\x01\x80")  # -1.0 clipped maps to -32767
        self.assertEqual(data[2:4], b"\x00\x00")
        self.assertEqual(data[4:6], b"\xff\x7f")

    def test_fanout_drop_oldest_and_limits(self):
        hub = AudioStreamHub(max_subscribers=2)
        first = hub.subscribe("s", "a")
        second = hub.subscribe("s", "a")
        with self.assertRaises(AudioStreamError):
            hub.subscribe("s", "a"), "third subscriber is refused"
        delivered = hub.publish("s", "a", [0.5] * 10)
        self.assertEqual(delivered, 2)
        self.assertEqual(first.blocks.get_nowait(), pcm16_bytes([0.5] * 10))
        # A slow subscriber loses its oldest blocks and counts the loss.
        for index in range(MAX_QUEUED_BLOCKS + 3):
            hub.publish("s", "a", [float(index)] * 4)
        self.assertEqual(first.blocks.qsize(), MAX_QUEUED_BLOCKS)
        self.assertEqual(first.dropped_blocks, 3)
        self.assertEqual(hub.publish("s", "other", [0.0] * 4), 0, "no subscribers, nothing queued")
        status = hub.status("s", "a")
        self.assertEqual(status["subscribers"], 2)
        self.assertEqual(status["dropped_blocks"], 7, "3 dropped by the drained subscriber, 4 by the full one")
        hub.unsubscribe("s", "a", first)
        hub.unsubscribe("s", "a", second)
        self.assertEqual(hub.status("s", "a")["subscribers"], 0)

    def test_block_size_limit(self):
        hub = AudioStreamHub()
        with self.assertRaises(AudioStreamError):
            hub.publish("s", "a", np.zeros(48_001))


class AudioStreamServiceMixin:
    def test_push_delivers_and_status_counts(self):
        stream = self.session.streams[0].id
        subscription = self.service.audio_stream_subscribe(self.session.id, stream)
        try:
            self.service.phonogram_push(
                self.session.id, {"stream_id": stream, "samples": [0.25] * 8})
            block = subscription.blocks.get(timeout=1)
            self.assertEqual(block, pcm16_bytes([0.25] * 8))
            status = self.service.audio_stream_status(self.session.id, stream)
            self.assertEqual(status["subscribers"], 1)
            self.assertEqual(status["max_subscribers"], MAX_SUBSCRIBERS)
        finally:
            self.service.audio_stream_unsubscribe(self.session.id, stream, subscription)

    def test_unknown_stream_refused(self):
        with self.assertRaises(ValueError):
            self.service.audio_stream_subscribe(self.session.id, "foreign")


class MemoryAudioStreamTests(AudioStreamServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=50)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class SQLiteAudioStreamTests(AudioStreamServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class AudioStreamHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        service = SessionService(store=SQLiteSessionStore(self.path))
        self.session = create_session(service)
        self.stream = self.session.streams[0].id
        service.close()
        self.server = create_server(host="127.0.0.1", port=0, session_database=self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.root = f"/api/v1/sessions/{self.session.id}/audio"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, path):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_status_over_http(self):
        status, envelope = self.request(self.root + f"/status?stream_id={self.stream}")
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["subscribers"], 0)

    def test_idle_disconnections_release_subscription_slots_promptly(self):
        for _ in range(5):
            sock = socket.create_connection(self.server.server_address, timeout=2)
            sock.sendall((f'GET {self.root}/stream?stream_id={self.stream} HTTP/1.1\r\n'
                          'Host: localhost\r\nConnection: close\r\n\r\n').encode())
            header = b''
            while b'\r\n\r\n' not in header: header += sock.recv(1)
            self.assertIn(b'200 OK', header)
            sock.shutdown(socket.SHUT_RDWR); sock.close()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                _, envelope = self.request(self.root + f'/status?stream_id={self.stream}')
                if envelope['data']['subscribers'] == 0: break
                time.sleep(0.02)
            self.assertEqual(envelope['data']['subscribers'], 0)

    def test_stream_delivers_pcm_blocks(self):
        # A raw socket reads the endless response; the test pushes frames
        # through the API and reads exactly the expected PCM bytes back.
        sock = socket.create_connection(self.server.server_address, timeout=5)
        try:
            sock.sendall(
                f"GET {self.root}/stream?stream_id={self.stream} HTTP/1.1\r\n"
                f"Host: localhost\r\nConnection: close\r\n\r\n".encode())
            header = b""
            while b"\r\n\r\n" not in header:
                chunk = sock.recv(1)
                if not chunk:
                    break
                header += chunk
            self.assertIn(b"200 OK", header)
            self.assertIn(b"Content-Type: audio/pcm", header)
            self.assertIn(b"X-Sample-Rate: 48000", header)
            # Give the subscription a moment to register, then push frames.
            time.sleep(0.2)
            connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
            connection.request("POST", self.root.replace("/audio", "/phonogram") + "/frames",
                               body=json.dumps({"stream_id": self.stream, "samples": [0.5] * 4}).encode(),
                               headers={"Content-Type": "application/json"})
            self.assertEqual(connection.getresponse().status, 200)
            connection.close()
            expected = pcm16_bytes([0.5] * 4)
            received = b""
            while len(received) < len(expected):
                block = sock.recv(len(expected) - len(received))
                if not block:
                    break
                received += block
            self.assertEqual(received, expected)
        finally:
            sock.close()

    def test_error_statuses(self):
        status, envelope = self.request(self.root + "/status?stream_id=foreign")
        self.assertEqual(status, 400)
        self.assertEqual(envelope["error"]["code"], "invalid_request")
        status, envelope = self.request("/api/v1/sessions/no-such-session/audio/status?stream_id=x")
        self.assertEqual(status, 404)
        self.assertEqual(envelope["error"]["code"], "session_not_found")

    def test_capability_advertised(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", "/api/v1/capabilities")
            response = connection.getresponse()
            envelope = json.loads(response.read())
        finally:
            connection.close()
        self.assertTrue(envelope["data"]["features"]["remote_audio_stream"])


if __name__ == "__main__":
    unittest.main()
