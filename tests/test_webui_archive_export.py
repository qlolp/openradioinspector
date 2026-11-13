"""WebUI: NDJSON snapshot выбранного сеанса без сборки архива в браузере."""

from __future__ import annotations

import http.client
import json
import re
import threading
import unittest

from radioinspector.web.server import STATIC_DIR, create_server


class WebUiArchiveExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server(host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address
        payload = {
            "name": "WebUI export",
            "streams": [{
                "device": "tinySA Ultra",
                "role": "spectrum_analyzer",
                "level_unit": "dBm",
                "capabilities": ["sweep"],
            }],
        }
        connection = http.client.HTTPConnection(cls.host, cls.port, timeout=5)
        connection.request("POST", "/api/v1/sessions", body=json.dumps(payload), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        cls.session = json.loads(response.read())["data"]
        connection.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.automatic_detection.close()
        cls.server.automatic_handoff.close()
        cls.server.automatic_completion.close()
        cls.server.control_scheduler.close()
        cls.server.worker_manager.close()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.server.session_service.close()

    def test_export_link_is_session_bound_and_streaming_endpoint_stays_read_only(self) -> None:
        html = (STATIC_DIR / "platform.html").read_text(encoding="utf-8")
        javascript = (STATIC_DIR / "archive-export.js").read_text(encoding="utf-8")
        self.assertIn('src="/static/archive-export.js"', html)
        self.assertIn('id="archive-export"', html)
        self.assertIn('aria-disabled="true"', html)
        self.assertIn("new MutationObserver", javascript)
        self.assertIn("/archive/export.ndjson", javascript)
        self.assertIn("encodeURIComponent(sessionId)", javascript)
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("outerHTML", javascript)
        self.assertNotIn("fetch(", javascript)
        self.assertEqual(set(re.findall(r'byId\("([^"]+)"\)', javascript)) - {"session-select", "archive-export"}, set())

        path = f"/api/v1/sessions/{self.session['id']}/archive/export.ndjson"
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Archive-Sweeps"), "0")
        self.assertEqual(response.getheader("X-Archive-Observations"), "0")
        self.assertIn("openradioinspector-archive-ndjson", body)
        self.assertNotIn("task_file", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
