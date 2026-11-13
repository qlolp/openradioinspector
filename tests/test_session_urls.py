"""HTTP-маршруты сохраняют идентификаторы сеансов после одного декодирования."""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from urllib.parse import quote

from radioinspector.session_service import SessionService
from radioinspector.web.server import create_server
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import session_payload


SESSION_IDS = (
    "a/b",
    "a%2Fb",
    "a%252Fb",
    "сеанс 📻",
    "with space",
    "100%",
    "a?b",
    "a#b",
    "a+b",
    "/",
)


class SessionUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SessionService()
        self.server = create_server(host="127.0.0.1", port=0, sessions=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.service.close()

    def request(
        self, method: str, path: str, payload: dict | None = None
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def create_session(self, session_id: str) -> dict:
        payload = session_payload()
        payload["id"] = session_id
        status, response = self.request("POST", "/api/v1/sessions", payload)
        self.assertEqual(status, 201)
        self.assertEqual(response["data"]["id"], session_id)
        return response["data"]

    def test_get_decodes_session_segment_exactly_once(self) -> None:
        for session_id in SESSION_IDS:
            self.create_session(session_id)
        for session_id in SESSION_IDS:
            with self.subTest(session_id=session_id):
                status, response = self.request(
                    "GET", "/api/v1/sessions/" + quote(session_id, safe="")
                )
                self.assertEqual(status, 200)
                self.assertEqual(response["data"]["id"], session_id)

    def test_start_and_stop_decode_session_segment_exactly_once(self) -> None:
        for session_id in SESSION_IDS:
            self.create_session(session_id)
        for session_id in SESSION_IDS:
            for action, state in (("start", "running"), ("stop", "stopped")):
                with self.subTest(session_id=session_id, action=action):
                    route = (
                        "/api/v1/sessions/"
                        + quote(session_id, safe="")
                        + "/"
                        + action
                    )
                    status, response = self.request("POST", route, {})
                    self.assertEqual(status, 200)
                    self.assertEqual(response["data"]["id"], session_id)
                    self.assertEqual(response["data"]["state"], state)

    def test_measurement_routes_use_decoded_id_for_body_and_query(self) -> None:
        for session_id in SESSION_IDS:
            session = self.create_session(session_id)
            for index, (kind, factory) in enumerate(
                (("sweeps", sweep_payload), ("observations", observation_payload))
            ):
                route = "/api/v1/sessions/" + quote(session_id, safe="") + "/" + kind
                stream_id = session["streams"][index]["id"]
                for include_id in (False, True):
                    with self.subTest(
                        session_id=session_id, kind=kind, include_id=include_id
                    ):
                        payload = factory(session_id, stream_id, int(include_id))
                        payload["id"] = f"{session_id}:{kind}:{int(include_id)}"
                        if not include_id:
                            del payload["session_id"]
                        status, response = self.request("POST", route, payload)
                        self.assertEqual(status, 201)
                        self.assertEqual(response["data"]["session_id"], session_id)
                with self.subTest(session_id=session_id, kind=kind, method="GET"):
                    status, response = self.request(
                        "GET", route + "?stream_id=" + quote(stream_id, safe="")
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(len(response["data"]), 2)
                    self.assertTrue(
                        all(item["session_id"] == session_id for item in response["data"])
                    )

    def test_encoded_body_id_is_not_equivalent_to_decoded_route_id(self) -> None:
        session = self.create_session("a/b")
        for index, (kind, factory) in enumerate(
            (("sweeps", sweep_payload), ("observations", observation_payload))
        ):
            with self.subTest(kind=kind):
                payload = factory("a%2Fb", session["streams"][index]["id"])
                status, response = self.request(
                    "POST", "/api/v1/sessions/a%2Fb/" + kind, payload
                )
                self.assertEqual(status, 400)
                self.assertEqual(response["error"]["code"], "invalid_request")
                self.assertEqual(self.service.list_sweeps("a/b"), [])
                self.assertEqual(self.service.list_observations("a/b"), [])

    def test_decoding_does_not_change_route_structure(self) -> None:
        self.create_session("a/b")
        for method, path in (
            ("GET", "/api/v1/sessions/a/b"),
            ("POST", "/api/v1/sessions/a/b/start"),
            ("GET", "/api/v1/sessions/a%2Fb/unknown"),
            ("POST", "/api/v1/sessions/a%2Fb/%73tart"),
            ("GET", "/api/v1/sessions/a%2Fb/%73weeps"),
            ("GET", "/api/v1/sessions/a%2Fb/sweeps/extra"),
            ("POST", "/api/v1/sessions/a%2Fb/start/extra"),
        ):
            with self.subTest(method=method, path=path):
                status, response = self.request(
                    method, path, {} if method == "POST" else None
                )
                self.assertEqual(status, 404)
                self.assertEqual(response["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
