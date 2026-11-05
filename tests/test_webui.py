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


if __name__ == "__main__":
    unittest.main()
