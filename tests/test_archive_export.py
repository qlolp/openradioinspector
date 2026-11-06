"""Проверки read-only экспорта и кругового переноса архива."""

from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from radioinspector.archive_export import (
    build_archive_document,
    fetch_archive_document,
    main,
)
from radioinspector.archive_ingest import (
    MAX_RESPONSE_BYTES,
    ArchiveIngestClient,
    ArchiveIngestError,
    import_archive,
    parse_archive_document,
)
from radioinspector.contracts import to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import session_payload


def creation_payload(session) -> dict:
    return {
        "id": session.id,
        "name": session.name,
        "streams": to_primitive(session.streams),
        "metadata": session.metadata,
    }


class ArchiveServiceExportTests(unittest.TestCase):
    def test_export_exceeds_http_listing_limit(self) -> None:
        service = SessionService()
        session = service.create_session(session_payload())
        stream_id = session.streams[0].id
        for start in range(0, 1001, 100):
            service.ingest_sweeps(
                [
                    sweep_payload(session.id, stream_id, number)
                    for number in range(start, min(start + 100, 1001))
                ]
            )
        document = build_archive_document(service, session.id)
        self.assertEqual(len(document["sweeps"]), 1001)
        self.assertEqual(document["observations"], [])
        self.assertEqual(len(service.list_sweeps(session.id)), 100)
        self.assertEqual(len(service.list_sweeps(session.id, limit=1000)), 1000)
        self.assertEqual(document["sweeps"][0]["id"], "sweep-0")
        self.assertEqual(document["sweeps"][-1]["id"], "sweep-1000")

    def test_export_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteSessionStore(str(Path(temporary) / "archive.sqlite"))
            service = SessionService(store=store)
            try:
                session = service.create_session(session_payload())
                spectrum_id, receiver_id = (item.id for item in session.streams)
                service.ingest_sweep(sweep_payload(session.id, spectrum_id))
                service.ingest_observation(observation_payload(session.id, receiver_id))
                before = service.health()
                document = build_archive_document(service, session.id)
                self.assertEqual(service.health(), before)
                self.assertEqual(document["sweeps"][0]["level_unit"], "dBm")
                self.assertEqual(
                    document["observations"][0]["level_unit"], "s_meter_count"
                )
                filtered = build_archive_document(
                    service, session.id, stream_id=receiver_id
                )
                self.assertEqual(filtered["sweeps"], [])
                self.assertEqual(len(filtered["observations"]), 1)
                with self.assertRaisesRegex(ValueError, "не принадлежит"):
                    build_archive_document(service, session.id, stream_id="foreign")
            finally:
                service.close()


class HttpNode:
    def __init__(self, database: str, *, port: int = 0) -> None:
        self.database = database
        self.requested_port = port
        self.service = None
        self.server = None
        self.thread = None
        self.start()

    def start(self) -> None:
        self.service = SessionService(store=SQLiteSessionStore(self.database))
        self.server = create_server(
            host="127.0.0.1", port=self.requested_port, sessions=self.service
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.url = f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.service is not None:
            self.service.close()
        self.server = None
        self.thread = None
        self.service = None

    def request(self, path: str) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw.decode("utf-8"))


class ArchiveHttpExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.node = HttpNode(str(Path(self.temporary.name) / "node.sqlite"))
        self.session = self.node.service.create_session(session_payload())

    def tearDown(self) -> None:
        self.node.stop()
        self.temporary.cleanup()

    def test_round_trip_between_independent_http_sqlite_nodes(self) -> None:
        spectrum_id, receiver_id = (item.id for item in self.session.streams)
        self.node.service.ingest_sweeps(
            [sweep_payload(self.session.id, spectrum_id, number) for number in range(2)]
        )
        self.node.service.ingest_observation(
            observation_payload(self.session.id, receiver_id)
        )
        before = self.node.service.health()
        client = ArchiveIngestClient(self.node.url)
        document = fetch_archive_document(client, self.session.id)
        self.assertEqual(self.node.service.health(), before)
        status, capabilities = self.node.request("/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(capabilities["data"]["features"]["session_archive_export"])
        self.assertTrue(
            capabilities["data"]["features"]["session_archive_export_archive_wide"]
        )

        other = HttpNode(str(Path(self.temporary.name) / "other.sqlite"))
        try:
            restored = other.service.create_session(creation_payload(self.session))
            self.assertEqual(restored.id, self.session.id)
            imported = import_archive(
                ArchiveIngestClient(other.url),
                parse_archive_document(document, self.session.id),
                self.session.id,
                batch_size=2,
            )
            self.assertEqual(imported["batches"], 2)
            other.stop()
            other.start()
            status, exported = other.request(
                f"/api/v1/sessions/{self.session.id}/archive/export"
            )
            self.assertEqual(status, 200)
            self.assertEqual(exported["data"], document)
            self.assertEqual(other.service.health()["last_event_id"], 4)
        finally:
            other.stop()

    def test_http_filters_empty_export_and_errors(self) -> None:
        client = ArchiveIngestClient(self.node.url)
        empty = fetch_archive_document(client, self.session.id)
        self.assertEqual(empty["sweeps"], [])
        self.assertEqual(empty["observations"], [])
        status, response = self.node.request(
            f"/api/v1/sessions/{self.session.id}/archive/export?stream_id=foreign"
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], "invalid_request")
        with self.assertRaises(ArchiveIngestError) as caught:
            fetch_archive_document(client, "missing/session")
        self.assertEqual(caught.exception.status, 404)

    def test_export_response_has_no_fixed_cap_but_ingest_keeps_limit(self) -> None:
        envelope = json.dumps(
            {
                "contract": "openradioinspector-platform",
                "contract_version": 1,
                "data": {
                    "format": "openradioinspector-archive",
                    "format_version": 1,
                    "sweeps": [],
                    "observations": [],
                },
            }
        ).encode("utf-8")
        raw = envelope[:-1] + b" " * (MAX_RESPONSE_BYTES + 1) + b"}"

        class Response:
            status = 200

            def __init__(self) -> None:
                self.read_size = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size=-1):
                self.read_size = size
                return raw

        class Opener:
            def __init__(self) -> None:
                self.response = Response()

            def open(self, _request, timeout):
                return self.response

        opener = Opener()
        client = ArchiveIngestClient(self.node.url, opener=opener)
        document = client.export_archive(self.session.id)
        self.assertEqual(document["format"], "openradioinspector-archive")
        self.assertEqual(opener.response.read_size, -1)

        opener = Opener()
        client = ArchiveIngestClient(self.node.url, opener=opener)
        with self.assertRaises(ArchiveIngestError) as caught:
            client.capabilities()
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertEqual(opener.response.read_size, MAX_RESPONSE_BYTES + 1)

    def test_invalid_response_does_not_create_output(self) -> None:
        output = Path(self.temporary.name) / "invalid.json"

        class InvalidClient:
            def export_archive(self, session_id, *, stream_id=None):
                return {"format": "other", "format_version": 1}

        with patch(
            "radioinspector.archive_export.ArchiveIngestClient",
            return_value=InvalidClient(),
        ), redirect_stderr(io.StringIO()) as errors:
            code = main(
                [
                    "--session",
                    self.session.id,
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("Ошибка экспорта архива", errors.getvalue())
        self.assertFalse(output.exists())

    def test_cli_writes_valid_document_to_stdout(self) -> None:
        service = self.node.service

        class EmptyClient:
            def export_archive(self, session_id, *, stream_id=None):
                return build_archive_document(
                    service, session_id, stream_id=stream_id
                )

        with patch(
            "radioinspector.archive_export.ArchiveIngestClient",
            return_value=EmptyClient(),
        ), redirect_stdout(io.StringIO()) as output:
            code = main(["--session", self.session.id])
        self.assertEqual(code, 0)
        document = json.loads(output.getvalue())
        self.assertEqual(document["format"], "openradioinspector-archive")
        self.assertEqual(document["sweeps"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
