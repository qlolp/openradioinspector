"""Original Python HTTP versus actual Core uploaded audio; no device access."""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[3]
if __name__ == "__main__" and not __package__:
    # Direct execution uses this checkout's original oracle and owned helper,
    # independently of the caller's working directory or installed tools.
    sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

from audio_http_oracle import SAMPLE_CASES, wav_frames

from tools.modernization.api_parity import (
    NodeSpec,
    OwnedNode,
    compare_value,
    file_sha256,
)


class UploadedAudioHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = Path(
            os.environ.get("ORI_CORE_TEST_BINARY", ROOT / "target/debug/ori-core")
        )
        if not binary.is_file():
            raise RuntimeError(
                "Build the selected ori-core before the uploaded audio HTTP gate"
            )
        cls.binary = binary.resolve()
        cls.digest = file_sha256(cls.binary)
        cls.began = datetime.now(timezone.utc).isoformat()
        cls.directory = Path(tempfile.mkdtemp(prefix="ori-audio-http-")).resolve()
        cls.nodes = []
        cls.next_case = 0
        try:
            for label, spec in (
                ("python", NodeSpec("python", Path(sys.executable), ROOT)),
                ("core", NodeSpec("core", cls.binary, ROOT)),
            ):
                node = OwnedNode(spec, cls.directory / label)
                cls.nodes.append(node)
                node.__enter__()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        failures = []
        for node in reversed(cls.nodes):
            try:
                node.close()
            except BaseException as error:  # noqa: BLE001 - close every owned fixture before reporting
                failures.append(str(error))
        if file_sha256(cls.binary) != cls.digest:
            failures.append("Selected immutable Core binary changed")
        if report := os.environ.get("ORI_AUDIO_HTTP_REPORT"):
            Path(report).write_text(
                json.dumps(
                    {
                        "schema": "ori-uploaded-audio-http-v1",
                        "actual_begin_utc": cls.began,
                        "actual_end_utc": datetime.now(timezone.utc).isoformat(),
                        "core_binary": str(cls.binary),
                        "core_sha256": cls.digest,
                        "fixtures": [
                            {
                                "kind": node.spec.kind,
                                "joined": node.joined,
                                "child_exit_code": None
                                if node.process is None
                                else node.process.returncode,
                                "output_overflow": node.log_overflow,
                                "ready_exists": node.ready.exists(),
                            }
                            for node in cls.nodes
                        ],
                        "cleanup_errors": failures,
                        "hardware_validation": "not_run",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        if failures:
            raise AssertionError(
                "Retained " + str(cls.directory) + ": " + "; ".join(failures)
            )
        import shutil

        shutil.rmtree(cls.directory)

    def setUp(self):
        fixture = json.loads(
            (ROOT / "contracts/fixtures/measurement-v1.json").read_text(
                encoding="utf-8"
            )
        )
        type(self).next_case += 1
        session_id = "audio-http-" + str(type(self).next_case)
        self.targets = []
        for node in self.nodes:
            created = node.request(
                "POST",
                "/api/v1/sessions",
                {
                    "id": session_id,
                    "name": "Synthetic uploaded PCM compatibility",
                    "streams": [
                        dict(
                            {
                                key: value
                                for key, value in stream.items()
                                if key not in ("id", "session_id", "state")
                            },
                            id=session_id + "-stream-" + str(index),
                        )
                        for index, stream in enumerate(fixture["session"]["streams"])
                    ],
                },
            )
            self.assertEqual(created.status, 201, created.body[:1024])
            session = created.json()["data"]
            self.assertEqual(session["id"], session_id)
            stream = session["streams"][0]["id"]
            self.assertEqual(stream, session_id + "-stream-0")
            base = "/api/v1/sessions/" + session["id"]
            self.targets.append((node, base, stream))

    def call(self, target, method, action, payload=None, *, stream=True):
        node, base, identifier = target
        path = base + action
        if method == "GET" and stream:
            path += "?" + urlencode({"stream_id": identifier})
        body = None if payload is None else dict(payload, stream_id=identifier)
        return node.request(method, path, body, maximum=64 * 1024 * 1024)

    def status_data(self, response):
        self.assertEqual(response.status, 200, response.body[:1024])
        return response.json()["data"]

    def assert_original_json(self, first, second, *, clocks=()):
        self.assertEqual(compare_value(first, second, volatile_paths=clocks), [])

    def test_initial_status_and_exact_original_errors(self):
        statuses, live_statuses = [], []
        for target in self.targets:
            capabilities = self.status_data(target[0].request("GET", "/api/v1/capabilities"))
            self.assertIs(capabilities["features"].get("phonogram_pre_history"), True)
            self.assertIs(capabilities["features"].get("remote_audio_stream"), True)
            data = self.status_data(self.call(target, "GET", "/phonogram/status"))
            self.assertEqual(data["sample_rate_hz"], 48000)
            self.assertIs(data["recording"], False)
            self.assertEqual(data["buffered_seconds"], 0.0)
            self.assertEqual(data["pre_history_seconds"], 5.0)
            self.assertIsNone(data["last"])
            live = self.status_data(self.call(target, "GET", "/audio/status"))
            statuses.append(data)
            live_statuses.append(live)
            self.assertEqual(
                (
                    live["subscribers"],
                    live["max_subscribers"],
                    live["queued_blocks"],
                    live["dropped_blocks"],
                ),
                (0, 4, 0, 0),
            )
        self.assert_original_json(*statuses)
        self.assert_original_json(*live_statuses)
        for action, payload in (
            ("/phonogram/stop", {}),
            ("/phonogram/frames", {"samples": []}),
            ("/phonogram/frames", {"samples": ["NaN"]}),
        ):
            responses = [
                self.call(target, "POST", action, payload) for target in self.targets
            ]
            self.assertEqual(responses[0].status, responses[1].status)
            self.assert_original_json(responses[0].json(), responses[1].json())
        wav = [self.call(target, "GET", "/phonogram/wav") for target in self.targets]
        self.assertEqual(wav[0].status, 400)
        self.assert_original_json(wav[0].json(), wav[1].json())

    def test_original_quantization_pre_history_and_complete_wav_bytes(self):
        expected_wavs = []
        starts, stops = [], []
        prefix = SAMPLE_CASES["clipping"] + SAMPLE_CASES["adjacent"]
        recorded = [0.0, 1.0000000000000002, -0.0, -0.25]
        for target in self.targets:
            node, _, _ = target
            event = node.request("GET", "/api/v1/health").json()["data"][
                "last_event_id"
            ]
            self.status_data(
                self.call(target, "POST", "/phonogram/frames", {"samples": prefix})
            )
            started = self.status_data(
                self.call(target, "POST", "/phonogram/start", {})
            )
            self.assertIs(started["recording"], True)
            starts.append(started)
            self.status_data(
                self.call(target, "POST", "/phonogram/frames", {"samples": recorded})
            )
            stopped = self.status_data(self.call(target, "POST", "/phonogram/stop", {}))
            self.assertIs(stopped["recording"], False)
            stops.append(stopped)
            self.assertEqual(stopped["last"]["pre_history_samples"], len(prefix))
            self.assertEqual(stopped["last"]["recorded_samples"], len(recorded))
            response = self.call(target, "GET", "/phonogram/wav")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["content-type"], "audio/wav")
            self.assertEqual(
                int(response.headers["content-length"]), len(response.body)
            )
            expected_wavs.append(response.body)
            self.assertEqual(
                node.request("GET", "/api/v1/health").json()["data"]["last_event_id"],
                event,
                "uploaded audio must not change committed measurement history",
            )
        self.assertEqual(expected_wavs[0], expected_wavs[1])
        self.assert_original_json(*starts, clocks=("/started_at",))
        self.assert_original_json(*stops, clocks=("/last/started_at", "/last/ended_at"))
        self.assertEqual(wav_frames(expected_wavs[1])[:3], (48000, 1, 2))

    def test_pcm_stream_headers_original_bytes_and_disconnect_release(self):
        for absolute in (False, True):
            with self.subTest(request_target="absolute" if absolute else "origin"):
                self.pcm_roundtrip(absolute)

    def pcm_roundtrip(self, absolute):
        connections = []
        try:
            for node, base, stream in self.targets:
                from urllib.parse import urlsplit

                address = urlsplit(node.url)
                connection = http.client.HTTPConnection(
                    address.hostname, address.port, timeout=5
                )
                connections.append((connection, None))
                target = base + "/audio/stream?" + urlencode({"stream_id": stream})
                if absolute:
                    target = node.url + target
                connection.request(
                    "GET",
                    target,
                    headers={"Connection": "close"},
                )
                response = connection.getresponse()
                connections[-1] = (connection, response)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "audio/pcm")
                self.assertEqual(response.getheader("X-Sample-Rate"), "48000")
                self.assertEqual(response.getheader("X-Sample-Format"), "pcm16le")
                self.assertIsNone(response.getheader("Content-Length"))
            samples = [-1.0, -0.0, 0.5, 1.0]
            for target in self.targets:
                self.status_data(
                    self.call(target, "POST", "/phonogram/frames", {"samples": samples})
                )
            received = [response.read(len(samples) * 2) for _, response in connections]
            self.assertEqual(received[0], received[1])
        finally:
            for connection, response in connections:
                if response is not None:
                    response.close()
                connection.close()
        for target in self.targets:
            deadline = time.monotonic() + 2
            while True:
                status = self.status_data(self.call(target, "GET", "/audio/status"))
                if status["subscribers"] == 0:
                    break
                self.assertLess(
                    time.monotonic(),
                    deadline,
                    "disconnected client retained its audio slot",
                )
                time.sleep(0.01)

    def test_original_numeric_shapes_and_errors_preserve_response_and_state(self):
        for samples in ([None], ["junk"], ["NaN"], ["inf"], [{}], [[0], [1, 2]]):
            replies = [
                self.call(target, "POST", "/phonogram/frames", {"samples": samples})
                for target in self.targets
            ]
            self.assertEqual(replies[0].status, replies[1].status, repr(samples))
            self.assert_original_json(replies[0].json(), replies[1].json())
        for samples in SAMPLE_CASES.values():
            replies = [
                self.call(target, "POST", "/phonogram/frames", {"samples": samples})
                for target in self.targets
            ]
            self.assertEqual(replies[0].status, 200, replies[0].body)
            self.assert_original_json(replies[0].json(), replies[1].json())
        # The original mutates history before rejecting this live block.
        replies = [
            self.call(target, "POST", "/phonogram/frames", {"samples": [[0] * 48_001]})
            for target in self.targets
        ]
        self.assertEqual(replies[0].status, 503)
        self.assert_original_json(replies[0].json(), replies[1].json())
        statuses = [
            self.status_data(self.call(target, "GET", "/phonogram/status"))
            for target in self.targets
        ]
        self.assert_original_json(*statuses)

    def test_five_second_history_trims_partial_head_exactly(self):
        initial = [0.125] * 17
        block = [-1, 0.25, 1, -0.5] * 1200
        wavs = []
        for target in self.targets:
            self.status_data(
                self.call(target, "POST", "/phonogram/frames", {"samples": initial})
            )
            for _ in range(50):
                self.status_data(
                    self.call(target, "POST", "/phonogram/frames", {"samples": block})
                )
            self.status_data(
                self.call(target, "POST", "/phonogram/frames", {"samples": [0.75] * 17})
            )
            status = self.status_data(self.call(target, "POST", "/phonogram/start", {}))
            self.assertEqual(status["buffered_seconds"], 5.0)
            status = self.status_data(self.call(target, "POST", "/phonogram/stop", {}))
            self.assertEqual(status["last"]["pre_history_samples"], 240_000)
            self.assertEqual(status["last"]["recorded_samples"], 0)
            wavs.append(self.call(target, "GET", "/phonogram/wav").body)
        self.assertEqual(wavs[0], wavs[1])
        self.assertEqual(len(wav_frames(wavs[1])[3]), 480_000)

    def test_stream_subscription_limit_and_idle_disconnect_release(self):
        from urllib.parse import urlsplit

        for target in self.targets:
            node, base, stream = target
            address = urlsplit(node.url)
            connections = []
            try:
                for expected in (200, 200, 200, 200, 409):
                    connection = http.client.HTTPConnection(
                        address.hostname, address.port, timeout=5
                    )
                    connections.append((connection, None))
                    connection.request(
                        "GET",
                        base + "/audio/stream?" + urlencode({"stream_id": stream}),
                        headers={"Connection": "close"},
                    )
                    response = connection.getresponse()
                    connections[-1] = (connection, response)
                    self.assertEqual(response.status, expected)
                    if expected == 409:
                        data = json.loads(response.read())
                        self.assertEqual(
                            data["error"]["code"], "audio_stream_unavailable"
                        )
                        self.assertEqual(
                            data["error"]["message"], "Не более 4 подписчиков на поток"
                        )
                self.assertEqual(
                    self.status_data(self.call(target, "GET", "/audio/status"))[
                        "subscribers"
                    ],
                    4,
                )
            finally:
                for connection, response in connections:
                    if response is not None:
                        response.close()
                    connection.close()
            deadline = time.monotonic() + 2
            while True:
                subscribers = self.status_data(
                    self.call(target, "GET", "/audio/status")
                )["subscribers"]
                if not subscribers or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertEqual(subscribers, 0)

    def test_original_validation_priority_missing_session_and_foreign_stream(self):
        for node, base, _ in self.targets:
            cases = [
                (
                    "POST",
                    base + "/phonogram/frames",
                    {"stream_id": "not-a-stream", "samples": ["NaN"]},
                    400,
                    "Поток не принадлежит сеансу: not-a-stream",
                ),
                (
                    "POST",
                    base + "/phonogram/start",
                    {"stream_id": "not-a-stream", "extra": True},
                    400,
                    "Разрешено только поле stream_id",
                ),
                (
                    "POST",
                    base + "/phonogram/frames",
                    {"stream_id": "not-a-stream", "samples": []},
                    400,
                    "samples должен содержать от 1 до 4800 отсчётов",
                ),
                (
                    "POST",
                    "/api/v1/sessions/missing/phonogram/start",
                    {"stream_id": " "},
                    400,
                    "stream_id должен быть непустым ID потока",
                ),
                (
                    "POST",
                    "/api/v1/sessions/missing/phonogram/start",
                    {"stream_id": "not-a-stream"},
                    404,
                    "Сеанс не найден: missing",
                ),
                (
                    "GET",
                    base + "/phonogram/status?stream_id=not-a-stream",
                    None,
                    400,
                    "Поток не принадлежит сеансу: not-a-stream",
                ),
                (
                    "GET",
                    base + "/audio/stream?stream_id=not-a-stream",
                    None,
                    409,
                    "Поток не принадлежит сеансу: not-a-stream",
                ),
                (
                    "GET",
                    "/api/v1/sessions/missing/audio/stream?stream_id=not-a-stream",
                    None,
                    404,
                    "'Сеанс не найден: missing'",
                ),
                (
                    "GET",
                    "/api/v1/sessions/missing'quote/audio/stream?stream_id=not-a-stream",
                    None,
                    404,
                    '"Сеанс не найден: missing\'quote"',
                ),
            ]
            for method, path, body, status, message in cases:
                response = node.request(method, path, body)
                self.assertEqual(response.status, status, response.body)
                self.assertEqual(response.json()["error"]["message"], message)


if __name__ == "__main__":
    unittest.main()
