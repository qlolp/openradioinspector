"""Потоковый NDJSON-экспорт с неизменным disk-backed snapshot."""

from __future__ import annotations

import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from radioinspector.archive_export_stream import (
    fetch_archive_ndjson,
    iter_archive_ndjson,
    main,
    open_archive_snapshot,
)
from radioinspector.archive_ingest import ArchiveIngestClient, ArchiveIngestError
from radioinspector.archive_stream import import_stream
from radioinspector.contracts import to_primitive
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_archive_export import HttpNode, creation_payload
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import session_payload


class ArchiveSnapshotTests(unittest.TestCase):
    def test_sqlite_streams_over_list_limit_without_archive_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = SessionService(
                store=SQLiteSessionStore(str(Path(temporary) / "archive.sqlite"))
            )
            try:
                session = service.create_session(session_payload())
                spectrum_id, receiver_id = (item.id for item in session.streams)
                for start in range(0, 1001, 100):
                    service.ingest_sweeps(
                        [
                            sweep_payload(session.id, spectrum_id, number)
                            for number in range(start, min(start + 100, 1001))
                        ]
                    )
                service.ingest_observation(
                    observation_payload(session.id, receiver_id)
                )
                before = service.health()
                with patch.object(
                    service,
                    "archive_measurements",
                    side_effect=AssertionError("archive must not be materialized"),
                ):
                    lines = b"".join(iter_archive_ndjson(service, session.id)).splitlines()
                self.assertEqual(len(lines), 1003)
                self.assertEqual(json.loads(lines[1])["measurement"]["id"], "sweep-0")
                self.assertEqual(json.loads(lines[-2])["measurement"]["id"], "sweep-1000")
                self.assertEqual(
                    json.loads(lines[-1])["measurement"]["level_unit"],
                    "s_meter_count",
                )
                self.assertEqual(service.health(), before)
            finally:
                service.close()

    def test_open_snapshot_ignores_later_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = SessionService(
                store=SQLiteSessionStore(str(Path(temporary) / "archive.sqlite"))
            )
            try:
                session = service.create_session(session_payload())
                spectrum_id = session.streams[0].id
                service.ingest_sweep(sweep_payload(session.id, spectrum_id, 0))
                with open_archive_snapshot(service, session.id) as snapshot:
                    service.ingest_sweep(sweep_payload(session.id, spectrum_id, 1))
                    self.assertEqual(snapshot.sweep_count, 1)
                    batches = list(snapshot.sweeps(batch_size=1))
                self.assertEqual(len(batches), 1)
                self.assertEqual(batches[0][0].id, "sweep-0")
                self.assertEqual(len(service.list_sweeps(session.id, limit=None)), 2)
            finally:
                service.close()


class ArchiveHttpStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.node = HttpNode(str(Path(self.temporary.name) / "node.sqlite"))
        self.session = self.node.service.create_session(session_payload())

    def tearDown(self) -> None:
        self.node.stop()
        self.temporary.cleanup()

    def test_http_round_trip_and_capabilities(self) -> None:
        spectrum_id, receiver_id = (item.id for item in self.session.streams)
        self.node.service.ingest_sweeps(
            [sweep_payload(self.session.id, spectrum_id, number) for number in range(2)]
        )
        self.node.service.ingest_observation(
            observation_payload(self.session.id, receiver_id)
        )
        before = self.node.service.health()
        output = Path(self.temporary.name) / "export.ndjson"
        result = fetch_archive_ndjson(
            ArchiveIngestClient(self.node.url), self.session.id, output
        )
        self.assertEqual((result["sweeps"], result["observations"]), (2, 1))
        self.assertEqual(self.node.service.health(), before)
        status, capabilities = self.node.request("/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(capabilities["data"]["features"]["session_archive_export_ndjson"])
        self.assertTrue(capabilities["data"]["features"]["session_archive_export_streaming"])

        other = HttpNode(str(Path(self.temporary.name) / "other.sqlite"))
        try:
            other.service.create_session(creation_payload(self.session))
            checkpoint = Path(self.temporary.name) / "resume.json"
            imported = import_stream(
                ArchiveIngestClient(other.url),
                output,
                self.session.id,
                checkpoint_path=checkpoint,
            )
            self.assertTrue(imported["complete"])
            resumed = import_stream(
                ArchiveIngestClient(other.url),
                output,
                self.session.id,
                checkpoint_path=checkpoint,
                resume=True,
            )
            self.assertTrue(resumed["complete"])
            self.assertEqual(resumed["batches"], 0)
            self.assertEqual(other.service.health()["last_event_id"], 4)
        finally:
            other.stop()

    def test_filter_empty_archive_and_http_errors(self) -> None:
        receiver_id = self.session.streams[1].id
        output = Path(self.temporary.name) / "empty.ndjson"
        result = fetch_archive_ndjson(
            ArchiveIngestClient(self.node.url),
            self.session.id,
            output,
            stream_id=receiver_id,
        )
        self.assertEqual((result["sweeps"], result["observations"]), (0, 0))
        self.assertEqual(len(output.read_text().splitlines()), 1)
        with self.assertRaises(ArchiveIngestError) as caught:
            fetch_archive_ndjson(
                ArchiveIngestClient(self.node.url),
                "missing/session",
                Path(self.temporary.name) / "missing.ndjson",
            )
        self.assertEqual(caught.exception.status, 404)

    def test_invalid_count_does_not_replace_existing_output(self) -> None:
        spectrum_id = self.session.streams[0].id
        self.node.service.ingest_sweep(sweep_payload(self.session.id, spectrum_id))
        output = Path(self.temporary.name) / "existing.ndjson"
        output.write_text("keep\n", encoding="utf-8")

        class Response(io.BytesIO):
            headers = {
                "Content-Type": "application/x-ndjson; charset=utf-8",
                "X-Archive-Sweeps": "0",
                "X-Archive-Observations": "0",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        body = (
            json.dumps(
                {
                    "format": "openradioinspector-archive-ndjson",
                    "format_version": 1,
                    "session_id": self.session.id,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "kind": "sweep",
                    "measurement": sweep_payload(self.session.id, spectrum_id),
                }
            )
            + "\n"
        ).encode()

        class Opener:
            def open(self, _request, timeout):
                return Response(body)

        client = ArchiveIngestClient(self.node.url, opener=Opener())
        with patch.object(client, "get_session", return_value=to_primitive(self.session)):
            with self.assertRaises(ArchiveIngestError) as caught:
                fetch_archive_ndjson(client, self.session.id, output)
        self.assertEqual(caught.exception.code, "incomplete_stream")
        self.assertEqual(output.read_text(), "keep\n")

    def test_main_reports_errors_without_output(self) -> None:
        output = Path(self.temporary.name) / "cli.ndjson"
        with patch(
            "radioinspector.archive_export_stream.ArchiveIngestClient",
            side_effect=ValueError("bad server"),
        ):
            code = main(["--session", self.session.id, "--output", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
