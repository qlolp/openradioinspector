"""WebUI: форма создания ControlTask без подделки status/outcome."""

from __future__ import annotations

import http.client
import re
import threading
import unittest

from radioinspector.web.server import STATIC_DIR, create_server


class WebUiTaskCreateTests(unittest.TestCase):
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

    def test_form_uses_create_api_and_refreshes_existing_task_list(self) -> None:
        html = (STATIC_DIR / "platform.html").read_text(encoding="utf-8")
        javascript = (STATIC_DIR / "task-create.js").read_text(encoding="utf-8")
        scheduler = (STATIC_DIR / "scheduler.js").read_text(encoding="utf-8")
        self.assertIn('src="/static/task-create.js"', html)
        for element_id in (
            "task-create-form", "task-create-name", "task-create-file",
            "task-create-start", "task-create-duration", "task-create-equipment",
            "task-create-submit", "task-create-status",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('method: "POST"', javascript)
        self.assertIn('/task-scheduler/tasks', javascript)
        self.assertIn("started.toISOString()", javascript)
        self.assertIn("ori:tasks-changed", javascript)
        self.assertIn("ori:tasks-changed", scheduler)
        self.assertIn("duration_s: duration", javascript)
        self.assertNotIn('"status":', javascript)
        self.assertNotIn('"outcome":', javascript)
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("outerHTML", javascript)
        ids = set(re.findall(r'id="([^"]+)"', html))
        requested = set(re.findall(r'byId\("([^"]+)"\)', javascript))
        self.assertEqual(requested - ids, set())

        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", "/static/task-create.js")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertTrue((response.getheader("Content-Type") or "").startswith("text/javascript"))
        self.assertEqual(body, javascript)


if __name__ == "__main__":
    unittest.main(verbosity=2)
