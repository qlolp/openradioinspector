"""Analysis fixture helpers use owned loopback sockets, without archive setup."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer

from tests import test_frequency_rounding, test_spectral_mask, test_sweep_three_thresholds


FIXTURES = (
    test_frequency_rounding.FrequencyRoundingHTTPTests,
    test_spectral_mask.MaskHTTPTests,
    test_sweep_three_thresholds.ThreeThresholdsHTTPTests,
)


class HTTPFixtureGetTests(unittest.TestCase):
    def capture(self, fixture, method, payload=None):
        observed = []

        class Capture(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass

            def capture(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                observed.append((self.command, self.path, self.headers, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = capture
            do_POST = capture

        # BaseHTTPRequestHandler needs no reverse-DNS HTTPServer metadata.
        with TCPServer(("127.0.0.1", 0), Capture) as server:
            server.timeout = 5
            worker = threading.Thread(target=server.handle_request, name="analysis-fixture-wire")
            # Exercise the existing helper directly. Its archive/server setUp
            # never runs; the only service is this owned ephemeral HTTP capture.
            case = fixture()
            case.server = server
            case.root = "/synthetic-analysis"
            worker.start()
            try:
                response = case.request(payload, method=method,
                                        path="/api/v1/capabilities" if method == "GET" else None)
            finally:
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "Owned HTTP capture did not stop")
            self.assertEqual(response, (200, {}))
            self.assertEqual(len(observed), 1)
            return observed[0]

    def test_get_requests_have_no_entity_on_wire(self):
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.__name__):
                method, path, headers, body = self.capture(fixture, "GET")
                self.assertEqual((method, path), ("GET", "/api/v1/capabilities"))
                self.assertEqual(body, b"", f"GET carried {len(body)} unread body bytes")
                for header in ("Content-Length", "Transfer-Encoding", "Content-Type"):
                    self.assertIsNone(headers.get_all(header), header)

    def test_post_requests_keep_json_on_wire(self):
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.__name__):
                method, path, headers, body = self.capture(
                    fixture, "POST", {"fixture": "synthetic", "frequency_hz": 102_500_003})
                self.assertEqual((method, path), ("POST", "/synthetic-analysis"))
                self.assertEqual(headers.get_all("Content-Type"), ["application/json"])
                self.assertIsNone(headers.get_all("Transfer-Encoding"))
                self.assertEqual(headers.get_all("Content-Length"), [str(len(body))])
                self.assertEqual(json.loads(body), {"fixture": "synthetic", "frequency_hz": 102_500_003})
