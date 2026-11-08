"""Phonogram recording with a fixed five-second pre-history window."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import wave
from io import BytesIO
from pathlib import Path

import numpy as np

from radioinspector.phonogram import (
    MAX_PHONOGRAM_SECONDS, MAX_PUSH_SAMPLES, PRE_HISTORY_SECONDS, SAMPLE_RATES,
    PhonogramError, PhonogramRecorder,
)
from radioinspector.session_service import SessionNotFoundError, SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_sweep_comparison import create_session

RATE = 48_000


def clock():
    from datetime import datetime, timezone
    return datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


class PhonogramRecorderTests(unittest.TestCase):
    def test_producer_buffer_reuse_cannot_modify_captured_audio(self):
        recorder = PhonogramRecorder(RATE, clock=clock)
        buffer = np.ones(100, dtype=np.float64)
        recorder.push(buffer); recorder.start()
        buffer[:] = 0.5; recorder.push(buffer)
        buffer[:] = 0
        captured = recorder.stop().samples
        np.testing.assert_array_equal(captured[:100], np.ones(100))
        np.testing.assert_array_equal(captured[100:], np.full(100, 0.5))

    def test_pre_history_window_is_exactly_five_seconds(self):
        recorder = PhonogramRecorder(RATE, clock=clock)
        self.assertEqual(recorder.pre_history_seconds, PRE_HISTORY_SECONDS)
        recorder.push(np.zeros(RATE * 7))
        self.assertAlmostEqual(recorder.buffered_seconds, 5.0)
        recorder.push(np.ones(RATE))
        self.assertAlmostEqual(recorder.buffered_seconds, 5.0)
        recorder.start()
        recorder.push(np.full(RATE, 0.5))
        phonogram = recorder.stop()
        self.assertAlmostEqual(phonogram.pre_history_seconds, 5.0)
        self.assertAlmostEqual(phonogram.recorded_seconds, 1.0)
        self.assertAlmostEqual(phonogram.duration_seconds, 6.0)
        # The last 4 seconds before start are zeros, then 1 second of ones.
        samples = phonogram.samples
        self.assertEqual(samples[0], 0.0)
        self.assertEqual(samples[4 * RATE], 1.0)
        self.assertEqual(samples[phonogram.pre_history_samples], 0.5)
        self.assertEqual(phonogram.recorded_samples, RATE)

    def test_empty_buffer_starts_with_zero_pre_history(self):
        recorder = PhonogramRecorder(RATE, clock=clock)
        started = recorder.start()
        self.assertAlmostEqual(started["pre_history_seconds"], 0.0)
        recorder.push(np.ones(10))
        phonogram = recorder.stop()
        self.assertEqual(phonogram.pre_history_samples, 0)
        self.assertEqual(phonogram.recorded_samples, 10)

    def test_state_machine_and_errors(self):
        recorder = PhonogramRecorder(RATE, clock=clock)
        with self.assertRaises(PhonogramError):
            recorder.stop(), "stop before start is refused"
        with self.assertRaises(PhonogramError):
            recorder.last_wav(), "no phonogram before the first stop"
        recorder.push(np.zeros(10))
        recorder.start()
        with self.assertRaises(PhonogramError):
            recorder.start(), "double start is refused"
        recorder.stop()
        for rate in (0, 44_100, -1):
            with self.subTest(rate=rate), self.assertRaises(PhonogramError):
                PhonogramRecorder(rate)
        with self.assertRaises(PhonogramError):
            PhonogramRecorder(RATE, pre_history_seconds=4.0)
        with self.assertRaises(PhonogramError):
            recorder.push([float("nan")])
        with self.assertRaises(PhonogramError):
            recorder.push(np.zeros(MAX_PUSH_SAMPLES + 1))

    def test_duration_cap_and_wav_roundtrip(self):
        recorder = PhonogramRecorder(8_000, clock=clock)
        recorder.push(np.zeros(8_000))
        recorder.start()
        # The cap refuses before the phonogram grows past MAX_PHONOGRAM_SECONDS.
        with self.assertRaises(PhonogramError):
            while True:
                recorder.push(np.zeros(4800))
        recorder2 = PhonogramRecorder(8_000, clock=clock)
        recorder2.start()
        recorder2.push(np.full(8_000, 0.5))
        phonogram = recorder2.stop()
        data = phonogram.wav_bytes()
        with wave.open(BytesIO(data)) as handle:
            self.assertEqual(handle.getframerate(), 8_000)
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getnframes(), phonogram.samples.size)
        self.assertEqual(recorder2.last_wav(), data)
        summary = phonogram.as_dict()
        self.assertEqual(set(summary), {"sample_rate_hz", "pre_history_samples",
                                        "recorded_samples", "pre_history_seconds",
                                        "recorded_seconds", "duration_seconds",
                                        "started_at", "ended_at"})


class PhonogramServiceMixin:
    def test_recorder_lifecycle_per_session_and_stream(self):
        stream = self.session.streams[0].id
        status = self.service.phonogram_push(
            self.session.id, {"stream_id": stream, "samples": [0.0] * 100})
        self.assertAlmostEqual(status["buffered_seconds"], 100 / RATE, places=6)
        started = self.service.phonogram_start(self.session.id, {"stream_id": stream})
        self.assertTrue(started["recording"])
        from radioinspector.windows.phonogram import validate_status
        validate_status(started, self.session.id, stream)
        self.service.phonogram_push(self.session.id, {"stream_id": stream, "samples": [0.5] * 100})
        result = self.service.phonogram_stop(self.session.id, {"stream_id": stream})
        validate_status(result, self.session.id, stream)
        self.assertEqual(result["last"]["pre_history_samples"], 100)
        self.assertEqual(result["last"]["recorded_samples"], 100)
        wav = self.service.phonogram_wav(self.session.id, stream)
        self.assertEqual(wav[:4], b"RIFF")

    def test_unknown_stream_and_session(self):
        with self.assertRaises(ValueError):
            self.service.phonogram_push(self.session.id, {"stream_id": "foreign", "samples": [0.0]})
        with self.assertRaises(SessionNotFoundError):
            self.service.phonogram_start("no-such-session", {"stream_id": "x"})
        with self.assertRaises(ValueError):
            self.service.phonogram_push(self.session.id, {"stream_id": self.session.streams[0].id, "samples": []})

    def test_capability(self):
        self.assertTrue(self.service.capabilities()["features"]["phonogram_pre_history"])


class MemoryPhonogramTests(PhonogramServiceMixin, unittest.TestCase):
    def setUp(self):
        self.service = SessionService(history_limit=50)
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class SQLitePhonogramTests(PhonogramServiceMixin, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "archive.sqlite")
        self.service = SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(self.service.close)
        self.session = create_session(self.service)


class PhonogramHTTPTests(unittest.TestCase):
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
        self.root = f"/api/v1/sessions/{self.session.id}/phonogram"

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        for name in ("automatic_completion", "automatic_detection", "automatic_handoff",
                     "control_scheduler", "worker_manager", "session_service"):
            getattr(self.server, name).close()
        self.server.server_close()

    def request(self, method, path, body=None, raw=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path,
                               body=None if body is None else json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read()
            return response.status, (data if raw else json.loads(data))
        finally:
            connection.close()

    def test_lifecycle_over_http_and_wav_download(self):
        status, envelope = self.request("POST", self.root + "/frames",
                                        {"stream_id": self.stream, "samples": [0.0] * 50})
        self.assertEqual(status, 200)
        status, envelope = self.request("POST", self.root + "/start", {"stream_id": self.stream})
        self.assertEqual(status, 200)
        self.assertTrue(envelope["data"]["recording"])
        self.request("POST", self.root + "/frames", {"stream_id": self.stream, "samples": [0.5] * 50})
        status, envelope = self.request("POST", self.root + "/stop", {"stream_id": self.stream})
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["last"]["pre_history_samples"], 50)
        self.assertEqual(envelope["data"]["last"]["recorded_samples"], 50)
        status, wav = self.request("GET", self.root + f"/wav?stream_id={self.stream}", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(wav[:4], b"RIFF")
        status, envelope = self.request("GET", self.root + f"/status?stream_id={self.stream}")
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["last"]["recorded_samples"], 50)

    def test_error_statuses(self):
        cases = [
            ("POST", self.root + "/frames", {"stream_id": "foreign", "samples": [0.0]}, 400, "invalid_request"),
            ("POST", self.root + "/frames", {"stream_id": self.stream, "samples": []}, 400, "invalid_request"),
            ("POST", self.root + "/start", {"stream_id": self.stream}, 200, None),
            ("POST", self.root + "/stop", {"stream_id": self.stream}, 200, None),
            ("POST", self.root + "/stop", {"stream_id": self.stream}, 409, "phonogram_error"),
        ]
        for method, path, body, expected, error in cases:
            with self.subTest(path=path):
                code, envelope = self.request(method, path, body)
                self.assertEqual(code, expected)
                if error:
                    self.assertEqual(envelope["error"]["code"], error)
        status, envelope = self.request("POST", "/api/v1/sessions/no-such-session/phonogram/start",
                                        {"stream_id": "x"})
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
        self.assertTrue(envelope["data"]["features"]["phonogram_pre_history"])


if __name__ == "__main__":
    unittest.main()
