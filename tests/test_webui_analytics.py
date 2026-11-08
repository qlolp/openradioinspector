"""Acceptance WebUI: archive-wide аналитика вызывается только по действию."""

from __future__ import annotations

import http.client
import re
import threading
import unittest

from radioinspector.web.server import STATIC_DIR, create_server


class WebUiAnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server(host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

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

    def test_analytics_asset_is_served_and_platform_refresh_excludes_it(self) -> None:
        html = (STATIC_DIR / "platform.html").read_text(encoding="utf-8")
        platform = (STATIC_DIR / "platform.js").read_text(encoding="utf-8")
        analytics = (STATIC_DIR / "analytics.js").read_text(encoding="utf-8")
        self.assertIn('src="/static/analytics.js"', html)
        self.assertNotIn('/analytics/summary', platform)
        self.assertNotIn('state.analytics', platform)
        self.assertIn('new MutationObserver', analytics)
        self.assertIn('/analytics/summary', analytics)
        self.assertIn('state.generation += 1', analytics)
        self.assertIn('state.pending = false;\n    state.generation += 1;', analytics)
        self.assertIn('Live-измерения продолжаются', analytics)
        self.assertNotIn('method: "POST"', analytics)
        self.assertNotIn("innerHTML", analytics)
        self.assertNotIn("outerHTML", analytics)

        ids = set(re.findall(r'id="([^"]+)"', html))
        requested = set(re.findall(r'byId\("([^"]+)"\)', analytics))
        self.assertEqual(requested - ids, set())
        self.assertIn("analytics-refresh", requested)

        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", "/static/analytics.js")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertTrue((response.getheader("Content-Type") or "").startswith("text/javascript"))
        self.assertEqual(body, (STATIC_DIR / "analytics.js").read_bytes().decode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
