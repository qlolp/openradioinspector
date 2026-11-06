"""Проверки внешнего импорта архива только через атомарный batch API."""

from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from radioinspector.archive_ingest import (
    ARCHIVE_FORMAT,
    ArchiveIngestClient,
    ArchiveIngestError,
    import_archive,
    load_archive_document,
    main,
    parse_archive_document,
)
from radioinspector.contracts import MonitoringSession, to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import session_payload


def make_session() -> dict:
    return to_primitive(MonitoringSession.from_dict(session_payload()))


def make_document(
    session_id: str,
    spectrum_id: str,
    receiver_id: str,
    *,
    sweep_count: int = 2,
    observation_count: int = 1,
) -> dict:
    return {
        "format": ARCHIVE_FORMAT,
        "format_version": 1,
        "sweeps": [
            sweep_payload(session_id, spectrum_id, number)
            for number in range(sweep_count)
        ],
        "observations": [
            observation_payload(session_id, receiver_id, number)
            for number in range(observation_count)
        ],
    }


class FakeClient:
    def __init__(self, session: dict, *, limit: int = 100, atomic: bool = True) -> None:
        self.session = session
        self.limit = limit
        self.atomic = atomic
        self.calls: list[tuple] = []

    def capabilities(self) -> dict:
        self.calls.append(("capabilities",))
        return {
            "features": {
                "measurement_batch_ingest": True,
                "measurement_batch_atomic": self.atomic,
                "measurement_batch_limit": self.limit,
            }
        }

    def get_session(self, session_id: str) -> dict:
        self.calls.append(("get_session", session_id))
        return self.session

    def ingest_batch(self, session_id: str, kind: str, items: list[dict]) -> dict:
        self.calls.append(("post", session_id, kind, items))
        return {"count": len(items), "items": items}


class ArchiveDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = make_session()
        self.session_id = self.session["id"]
        self.spectrum_id = self.session["streams"][0]["id"]
        self.receiver_id = self.session["streams"][1]["id"]

    def document(self, **kwargs) -> dict:
        return make_document(
            self.session_id, self.spectrum_id, self.receiver_id, **kwargs
        )

    def test_document_preserves_raw_units_integer_hz_and_stable_ids(self) -> None:
        payload = self.document()
        del payload["sweeps"][0]["session_id"]
        document = load_archive_document(
            io.StringIO(json.dumps(payload)), self.session_id
        )
        self.assertEqual(document.total, 3)
        self.assertEqual(document.sweeps[0].session_id, self.session_id)
        self.assertEqual(document.sweeps[0].frequencies_hz, (100_000_000, 100_100_000))
        self.assertEqual(document.sweeps[0].level_unit, "dBm")
        self.assertEqual(document.observations[0].level_unit, "s_meter_count")
        self.assertEqual(document.observations[0].level, 37.0)
        self.assertEqual(document.observations[0].id, "observation-0")

    def test_rejects_missing_duplicate_and_fractional_values(self) -> None:
        cases = []
        missing = self.document()
        del missing["sweeps"][0]["id"]
        cases.append((missing, "missing_id"))
        duplicate = self.document(sweep_count=2)
        duplicate["sweeps"][1]["id"] = duplicate["sweeps"][0]["id"]
        cases.append((duplicate, "duplicate_id"))
        fractional = self.document()
        fractional["sweeps"][0]["frequencies_hz"][0] = 100_000_000.5
        cases.append((fractional, "invalid_measurement"))
        mismatch = self.document()
        mismatch["observations"][0]["session_id"] = "other"
        cases.append((mismatch, "session_mismatch"))
        wrong_version = self.document()
        wrong_version["format_version"] = 1.0
        cases.append((wrong_version, "unsupported_format"))
        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ArchiveIngestError) as caught:
                    parse_archive_document(payload, self.session_id)
                self.assertEqual(caught.exception.code, code)

    def test_rejects_empty_unknown_fields_and_non_finite_json(self) -> None:
        empty = self.document(sweep_count=0, observation_count=0)
        with self.assertRaises(ArchiveIngestError) as caught:
            parse_archive_document(empty, self.session_id)
        self.assertEqual(caught.exception.code, "empty_document")
        unknown = self.document()
        unknown["session_id"] = self.session_id
        with self.assertRaises(ArchiveIngestError):
            parse_archive_document(unknown, self.session_id)
        raw = json.dumps(self.document()).replace('"source": "test"', '"source": NaN')
        with self.assertRaises(ArchiveIngestError) as caught:
            load_archive_document(io.StringIO(raw), self.session_id)
        self.assertEqual(caught.exception.code, "invalid_json")


class ArchivePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = make_session()
        self.session_id = self.session["id"]
        self.spectrum_id = self.session["streams"][0]["id"]
        self.receiver_id = self.session["streams"][1]["id"]

    def document(self, **kwargs):
        return parse_archive_document(
            make_document(
                self.session_id, self.spectrum_id, self.receiver_id, **kwargs
            ),
            self.session_id,
        )

    def test_batch_size_uses_server_limit(self) -> None:
        client = FakeClient(self.session, limit=2)
        result = import_archive(
            client,
            self.document(sweep_count=5, observation_count=0),
            self.session_id,
        )
        posts = [call for call in client.calls if call[0] == "post"]
        self.assertEqual([len(call[3]) for call in posts], [2, 2, 1])
        self.assertEqual(result["batches"], 3)
        self.assertEqual(result["batch_size"], 2)
        self.assertFalse(result["whole_file_atomic"])

    def test_requested_smaller_batch_size_preserves_order(self) -> None:
        client = FakeClient(self.session, limit=100)
        result = import_archive(
            client,
            self.document(sweep_count=3, observation_count=2),
            self.session_id,
            batch_size=1,
        )
        posts = [call for call in client.calls if call[0] == "post"]
        self.assertEqual(result["batches"], 5)
        self.assertEqual([call[2] for call in posts], ["sweeps"] * 3 + ["observations"] * 2)
        self.assertEqual(
            [call[3][0]["id"] for call in posts],
            ["sweep-0", "sweep-1", "sweep-2", "observation-0", "observation-1"],
        )
        with self.assertRaises(ArchiveIngestError):
            import_archive(
                FakeClient(self.session, limit=2),
                self.document(sweep_count=1, observation_count=0),
                self.session_id,
                batch_size=3,
            )

    def test_invalid_later_record_sends_no_posts(self) -> None:
        payload = make_document(
            self.session_id,
            self.spectrum_id,
            self.receiver_id,
            sweep_count=3,
            observation_count=0,
        )
        payload["sweeps"][2]["level_unit"] = "s_meter_count"
        document = parse_archive_document(payload, self.session_id)
        client = FakeClient(self.session, limit=1)
        with self.assertRaises(ArchiveIngestError) as caught:
            import_archive(client, document, self.session_id)
        self.assertEqual(caught.exception.code, "unit_mismatch")
        self.assertFalse(any(call[0] == "post" for call in client.calls))

    def test_foreign_stream_sends_no_posts(self) -> None:
        payload = make_document(
            self.session_id,
            self.spectrum_id,
            self.receiver_id,
            sweep_count=1,
            observation_count=0,
        )
        payload["sweeps"][0]["stream_id"] = "foreign"
        document = parse_archive_document(payload, self.session_id)
        client = FakeClient(self.session)
        with self.assertRaises(ArchiveIngestError) as caught:
            import_archive(client, document, self.session_id)
        self.assertEqual(caught.exception.code, "foreign_stream")
        self.assertFalse(any(call[0] == "post" for call in client.calls))

    def test_dry_run_sends_no_posts(self) -> None:
        client = FakeClient(self.session, limit=2)
        result = import_archive(
            client,
            self.document(sweep_count=3, observation_count=1),
            self.session_id,
            dry_run=True,
        )
        self.assertEqual(
            client.calls, [("capabilities",), ("get_session", self.session_id)]
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["batches"], 3)
        self.assertEqual(result["sweeps"], 3)
        self.assertEqual(result["observations"], 1)

    def test_atomic_capability_is_mandatory(self) -> None:
        client = FakeClient(self.session, atomic=False)
        with self.assertRaises(ArchiveIngestError) as caught:
            import_archive(client, self.document(), self.session_id)
        self.assertEqual(caught.exception.code, "batch_not_atomic")
        self.assertEqual(client.calls, [("capabilities",)])

    def test_batch_response_ids_are_verified(self) -> None:
        client = FakeClient(self.session)
        original = client.ingest_batch

        def wrong_response(session_id, kind, items):
            original(session_id, kind, items)
            return {"count": len(items), "items": [{"id": "other"} for _ in items]}

        client.ingest_batch = wrong_response
        with self.assertRaises(ArchiveIngestError) as caught:
            import_archive(client, self.document(), self.session_id)
        self.assertEqual(caught.exception.code, "invalid_batch_response")

    def test_server_url_is_validated(self) -> None:
        for url in ("file:///tmp/archive", "http://user:secret@localhost", "http://localhost/?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ArchiveIngestClient(url)
        client = ArchiveIngestClient("http://localhost:8770/api/v1/")
        self.assertEqual(client.base_url, "http://localhost:8770")


class ArchiveHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temporary.name) / "archive.sqlite")
        self.service = None
        self.server = None
        self.thread = None
        self.start_server()
        self.session = self.service.create_session(session_payload())
        self.session_id = self.session.id
        self.spectrum_id = self.session.streams[0].id
        self.receiver_id = self.session.streams[1].id
        self.base_url = f"http://{self.host}:{self.port}"

    def start_server(self) -> None:
        self.service = SessionService(store=SQLiteSessionStore(self.database))
        self.server = create_server(host="127.0.0.1", port=0, sessions=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def stop_server(self) -> None:
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

    def tearDown(self) -> None:
        self.stop_server()
        self.temporary.cleanup()

    def request(self, method: str, path: str) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request(method, path)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw.decode("utf-8"))

    def document(self, **kwargs) -> dict:
        return make_document(
            self.session_id, self.spectrum_id, self.receiver_id, **kwargs
        )

    def test_real_http_sqlite_import_and_restart(self) -> None:
        document = parse_archive_document(
            self.document(sweep_count=5, observation_count=2), self.session_id
        )
        client = ArchiveIngestClient(f"{self.base_url}/api/v1")
        result = import_archive(
            client, document, self.session_id, batch_size=2
        )
        self.assertEqual(result["batches"], 4)
        self.assertEqual(result["sweeps"], 5)
        self.assertEqual(result["observations"], 2)
        self.stop_server()
        self.start_server()
        status, sweeps = self.request(
            "GET", f"/api/v1/sessions/{self.session_id}/sweeps?limit=1000"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(sweeps["data"]), 5)
        self.assertTrue(all(item["level_unit"] == "dBm" for item in sweeps["data"]))
        status, observations = self.request(
            "GET", f"/api/v1/sessions/{self.session_id}/observations?limit=1000"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(observations["data"]), 2)
        self.assertTrue(
            all(item["level_unit"] == "s_meter_count" for item in observations["data"])
        )
        status, history = self.request("GET", "/api/v1/events/history?after=1&limit=1000")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["data"]), 7)

    def test_real_http_error_is_typed_and_writes_nothing(self) -> None:
        document = parse_archive_document(self.document(), self.session_id)
        client = ArchiveIngestClient(self.base_url)
        with self.assertRaises(ArchiveIngestError) as caught:
            import_archive(client, document, "missing/session")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "session_not_found")
        self.assertEqual(self.service.list_sweeps(self.session_id), [])

    def test_cli_dry_run_uses_real_preflight_without_posts(self) -> None:
        path = Path(self.temporary.name) / "input.json"
        path.write_text(json.dumps(self.document()), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "--server",
                    self.base_url,
                    "--session",
                    self.session_id,
                    "--input",
                    str(path),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.service.list_sweeps(self.session_id), [])
        self.assertEqual(self.service.list_observations(self.session_id), [])

    def test_cli_reports_input_error_without_traceback(self) -> None:
        path = Path(self.temporary.name) / "invalid.json"
        path.write_text("{}", encoding="utf-8")
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = main(
                [
                    "--server",
                    self.base_url,
                    "--session",
                    self.session_id,
                    "--input",
                    str(path),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("Ошибка импорта архива", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
