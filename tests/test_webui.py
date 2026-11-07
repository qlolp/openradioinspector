"""Статические маршруты и автономные ресурсы Raspberry Pi WebUI."""

from __future__ import annotations

import http.client
import re
import threading
import tomllib
import unittest
from html.parser import HTMLParser
from pathlib import Path

from radioinspector.web.server import STATIC_DIR, create_server


ROOT = Path(__file__).resolve().parents[1]


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "script" and values.get("src"):
            self.references.append(str(values["src"]))
        if tag == "link" and values.get("href"):
            self.references.append(str(values["href"]))
        if tag == "a" and values.get("href"):
            self.references.append(str(values["href"]))


class WebUiTests(unittest.TestCase):
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
        cls.server.worker_manager.close()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.server.session_service.close()

    def request(self, path: str) -> tuple[int, str, str]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        result = response.status, response.getheader("Content-Type") or "", body
        connection.close()
        return result

    def test_platform_route_preserves_legacy_root(self) -> None:
        for route in ("/platform", "/platform.html", "/platform?from=test"):
            with self.subTest(route=route):
                status, content_type, body = self.request(route)
                self.assertEqual(status, 200)
                self.assertTrue(content_type.startswith("text/html"))
                self.assertIn('id="candidate-form"', body)
                self.assertIn('src="/static/platform.js"', body)

        status, _, legacy = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn('src="/static/app.js"', legacy)
        self.assertNotIn('id="candidate-form"', legacy)

    def test_assets_are_local_and_packaged(self) -> None:
        html = (STATIC_DIR / "platform.html").read_text(encoding="utf-8")
        parser = _AssetParser()
        parser.feed(html)
        self.assertTrue(parser.references)
        for reference in parser.references:
            self.assertFalse(
                reference.startswith(("http://", "https://", "//")),
                f"Внешняя зависимость WebUI: {reference}",
            )

        for name, content_type in (("platform.css", "text/css"), ("platform.js", "text/javascript")):
            with self.subTest(asset=name):
                status, actual_type, body = self.request(f"/static/{name}")
                self.assertEqual(status, 200)
                self.assertTrue(actual_type.startswith(content_type))
                self.assertEqual(body, (STATIC_DIR / name).read_text(encoding="utf-8"))

        with (ROOT / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)
        package_data = project["tool"]["setuptools"]["package-data"]
        self.assertIn("static/*", package_data["radioinspector.web"])

    def test_javascript_uses_existing_elements_and_text_dom(self) -> None:
        parser = _AssetParser()
        parser.feed((STATIC_DIR / "platform.html").read_text(encoding="utf-8"))
        javascript = (STATIC_DIR / "platform.js").read_text(encoding="utf-8")
        requested_ids = set(re.findall(r'byId\("([^"]+)"\)', javascript))
        self.assertTrue(requested_ids)
        self.assertEqual(requested_ids - parser.ids, set())
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("outerHTML", javascript)
        self.assertNotIn("Math.random", javascript)
        self.assertIn('const API_ROOT = "/api/v1"', javascript)
        self.assertIn('/investigations/next', javascript)
        self.assertIn('/investigations/start-next', javascript)
        self.assertIn('confirm: true', javascript)
        self.assertIn('/automatic-handoff', javascript)
        self.assertIn('automatic_handoff.policy_changed', javascript)
        self.assertIn('automatic_handoff.started', javascript)
        self.assertIn('automatic_handoff.disabled', javascript)
        self.assertIn('payload.receiver_stream_id = dom.automaticReceiver.value || null', javascript)
        self.assertIn('/automatic-detection', javascript)
        self.assertIn('automatic_detection.policy_changed', javascript)
        self.assertIn('automatic_detection.candidate_created', javascript)
        self.assertIn('payload.stream_id = dom.detectionStream.value || null', javascript)
        self.assertIn('payload.threshold_dbm = requiredNumber', javascript)

    def test_automatic_completion_form_is_opt_in_and_isolated(self) -> None:
        html = (STATIC_DIR / "platform.html").read_text(encoding="utf-8")
        javascript = (STATIC_DIR / "completion.js").read_text(encoding="utf-8")
        self.assertIn('src="/static/completion.js"', html)
        for element_id in (
            "completion-form", "completion-status", "completion-interval",
            "completion-listening", "completion-observations", "completion-receiver",
            "completion-save", "completion-enable", "completion-disable",
        ):
            self.assertIn(f'id="{element_id}"', html)
            self.assertIn(f'byId("{element_id}")', javascript)
        self.assertIn('/automatic-completion', javascript)
        self.assertIn('configureAutomaticCompletion(enabled, saveOnly = false)', javascript)
        self.assertIn('if (!saveOnly) payload.enabled = enabled', javascript)
        self.assertIn('configureAutomaticCompletion(enabled, true)', javascript)
        self.assertIn('configureAutomaticCompletion(false)', javascript)
        self.assertIn('state.automaticCompletion = status', javascript)
        self.assertIn('state.automaticCompletion = null', javascript)
        self.assertIn('state.completionFormDirty = false', javascript)
        self.assertIn('Live-измерения и другие политики продолжаются', javascript)
        self.assertIn('automatic_completion.completed', javascript)
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("outerHTML", javascript)

    def test_task_scheduler_panel_is_global_opt_in_and_isolated(self) -> None:
        html = (STATIC_DIR / "platform.html").read_text(encoding="utf-8")
        javascript = (STATIC_DIR / "scheduler.js").read_text(encoding="utf-8")
        self.assertIn('src="/static/scheduler.js"', html)
        for element_id in (
            "task-scheduler-editor", "task-scheduler-status", "task-scheduler-total",
            "task-scheduler-running", "task-scheduler-completed",
            "task-scheduler-enable", "task-scheduler-disable",
        ):
            self.assertIn(f'id="{element_id}"', html)
            self.assertIn(f'byId("{element_id}")', javascript)
        self.assertIn('Runtime-only · по умолчанию выключен', html)
        self.assertIn('не связан с выбранным сеансом', html)
        self.assertIn('api("/task-scheduler")', javascript)
        self.assertIn('body: { enabled }', javascript)
        self.assertIn('state.actionPending', javascript)
        self.assertIn('state.generation', javascript)
        self.assertIn('Остальные панели продолжают работать', javascript)
        self.assertNotIn('interval_seconds', javascript)
        self.assertNotIn('automatic-completion', javascript)
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("outerHTML", javascript)
        status, content_type, body = self.request("/static/scheduler.js")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/javascript"))
        self.assertEqual(body, javascript)


if __name__ == "__main__":
    unittest.main()
