"""Published read aliases preserve exact IDs and do not alter session state."""
import http.client
import json
import struct
import threading
import unittest
from urllib.parse import quote

from radioinspector.contracts import to_primitive
from radioinspector.web.server import create_server
from tests.test_platform import session_payload


class SessionReadAliasTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server(host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.sessions = self.server.session_service
        payload = session_payload()
        payload["id"] = "session/é"
        payload["streams"][0]["id"] = "spectrum/%2F-e\u0301"
        self.session = self.sessions.create_session(payload)
        self.base = "/api/v1/sessions/" + quote(self.session.id, safe="")
        self.candidate = self.sessions.create_candidate(self.session.id, {
            "id": "candidate/%2F-e\u0301", "source_stream_id": self.session.streams[0].id,
            "interval": {"started_at": "2026-11-11T12:00:00.123456Z", "ended_at": "2026-11-11T12:00:00.123457Z"},
            "center_frequency_hz": 100000003, "bandwidth_hz": 1,
            "peak_level": -0.0, "score": 1.0000000000000002, "level_unit": "dBm",
        })
        self.job = self.sessions.enqueue_investigation(self.session.id, self.candidate.id, {
            "id": "job/%2F-e\u0301", "target_stream_id": self.session.streams[1].id, "mode": "FM",
        })
        self.before = to_primitive(self.sessions.events())

    def get(self, path):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_candidate_alias_matches_direct_route_and_preserves_float_bits(self):
        tail = quote(self.candidate.id, safe="")
        direct = self.get(self.base + "/candidates/" + tail)
        alias = self.get(self.base + "/candidates/by-id/" + tail)
        self.assertEqual(alias, direct)
        self.assertEqual(alias[0], 200)
        self.assertEqual(alias[1]["data"]["id"].encode(), self.candidate.id.encode())
        self.assertEqual(struct.pack(">d", alias[1]["data"]["peak_level"]), struct.pack(">d", -0.0))
        self.assertEqual(to_primitive(self.sessions.events()), self.before)

    def test_investigation_alias_decodes_the_final_id_exactly_once(self):
        tail = quote(self.job.id, safe="")
        alias = self.get(self.base + "/investigations/by-id/" + tail)
        self.assertEqual(alias, self.get(self.base + "/investigations/" + tail))
        self.assertEqual(alias[0], 200)
        self.assertEqual(alias[1]["data"]["id"].encode(), self.job.id.encode())
        self.assertEqual(to_primitive(self.sessions.events()), self.before)

    def test_published_stream_get_returns_original_stream_without_device_actions(self):
        status, envelope = self.get(self.base + "/streams/" + quote(self.session.streams[0].id, safe=""))
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"], to_primitive(self.session.streams[0]))
        self.assertEqual(to_primitive(self.sessions.events()), self.before)

    def test_missing_scopes_and_extra_segments_remain_read_only_errors(self):
        for path in ("/api/v1/sessions/absent/candidates/by-id/absent",
                     self.base + "/candidates/by-id/absent",
                     self.base + "/investigations/by-id/absent",
                     self.base + "/streams/absent",
                     self.base + "/candidates/by-id/absent/extra"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)
        self.assertEqual(to_primitive(self.sessions.events()), self.before)
