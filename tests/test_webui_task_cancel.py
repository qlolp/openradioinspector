"""WebUI: отмена queued ControlTask через существующий cancel API."""

from __future__ import annotations

import http.client
import threading
import unittest

from radioinspector.web.server import STATIC_DIR, create_server


class WebUiTaskCancelTests(unittest.TestCase):
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

    def test_cancel_button_is_limited_to_queue_and_uses_name_segment(self) -> None:
        html = (STATIC_DIR / "platform.html").read_text(encoding="utf-8")
        javascript = (STATIC_DIR / "scheduler.js").read_text(encoding="utf-8")
        self.assertIn('src="/static/scheduler.js"', html)
        self.assertIn('task.status === "В очереди на выполнение"', javascript)
        self.assertIn("cancelTask(task.name)", javascript)
        self.assertIn("encodeURIComponent(name)", javascript)
        self.assertIn("/cancel", javascript)
        self.assertIn("state.actionPending = false;\n      await refreshScheduler();", javascript)
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("outerHTML", javascript)

        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", "/static/scheduler.js")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertTrue((response.getheader("Content-Type") or "").startswith("text/javascript"))
        self.assertEqual(body, javascript)


if __name__ == "__main__":
    unittest.main(verbosity=2)
