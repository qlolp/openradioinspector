"""Двухпроходный NDJSON-импорт и подтверждённый resume checkpoint."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from radioinspector.archive_export import build_archive_document
from radioinspector.archive_ingest import ArchiveIngestClient, ArchiveIngestError
from radioinspector.archive_stream import STREAM_FORMAT, import_stream
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_archive_export import HttpNode
from tests.test_archive_ingest import make_session
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import session_payload


def write_stream(
    path: Path,
    session: dict,
    *,
    sweep_count: int = 3,
    observation_count: int = 2,
) -> None:
    session_id = session["id"]
    spectrum_id = session["streams"][0]["id"]
    receiver_id = session["streams"][1]["id"]
    with path.open("w", encoding="utf-8") as target:
        target.write(json.dumps({
            "format": STREAM_FORMAT,
            "format_version": 1,
            "session_id": session_id,
        }) + "\n")
        for number in range(sweep_count):
            target.write(json.dumps({
                "kind": "sweep",
                "measurement": sweep_payload(session_id, spectrum_id, number),
            }) + "\n")
        for number in range(observation_count):
            target.write(json.dumps({
                "kind": "observation",
                "measurement": observation_payload(session_id, receiver_id, number),
            }) + "\n")


class FakeClient:
    def __init__(self, session: dict, *, limit: int = 2) -> None:
        self.session = session
        self.limit = limit
        self.calls = []

    def capabilities(self) -> dict:
        self.calls.append(("capabilities",))
        return {"features": {
            "measurement_batch_ingest": True,
            "measurement_batch_atomic": True,
            "measurement_batch_limit": self.limit,
        }}

    def get_session(self, session_id: str) -> dict:
        self.calls.append(("session", session_id))
        return self.session

    def ingest_batch(self, session_id: str, kind: str, items: list[dict]) -> dict:
        self.calls.append(("post", kind, items))
        return {"count": len(items), "items": items}

    @property
    def posts(self) -> list:
        return [item for item in self.calls if item[0] == "post"]


class ArchiveStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.session = make_session()
        self.path = Path(self.temporary.name) / "archive.ndjson"
        write_stream(self.path, self.session, sweep_count=5, observation_count=2)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_invalid_last_line_sends_no_posts(self) -> None:
        with self.path.open("a", encoding="utf-8") as target:
            target.write(json.dumps({
                "kind": "observation",
                "measurement": observation_payload(
                    self.session["id"], self.session["streams"][1]["id"], 99
                ),
            }) + "\n")
        payload = json.loads(self.path.read_text().splitlines()[-1])
        payload["measurement"]["level_unit"] = "dBm"
        lines = self.path.read_text().splitlines()
        lines[-1] = json.dumps(payload)
        self.path.write_text("\n".join(lines) + "\n")
        client = FakeClient(self.session, limit=1)
        with self.assertRaises(ArchiveIngestError) as caught:
            import_stream(client, self.path, self.session["id"], batch_size=1)
        self.assertEqual(caught.exception.code, "unit_mismatch")
        self.assertEqual(client.posts, [])

    def test_resume_skips_confirmed_batches(self) -> None:
        checkpoint = Path(self.temporary.name) / "resume.json"
        first = FakeClient(self.session)
        result = import_stream(
            first, self.path, self.session["id"], checkpoint_path=checkpoint
        )
        self.assertEqual(result["batches"], 4)
        self.assertTrue(result["complete"])
        self.assertEqual(len(first.posts), 4)

        state = json.loads(checkpoint.read_text())
        state.update({
            "next_line": 4,
            "sweeps": 2,
            "observations": 0,
            "batches": 1,
            "complete": False,
        })
        checkpoint.write_text(json.dumps(state))
        second = FakeClient(self.session)
        resumed = import_stream(
            second,
            self.path,
            self.session["id"],
            checkpoint_path=checkpoint,
            resume=True,
        )
        self.assertEqual(resumed["sweeps"], 3)
        self.assertEqual(resumed["observations"], 2)
        self.assertEqual(resumed["batches"], 3)
        self.assertEqual(resumed["total_sweeps"], 5)
        self.assertEqual(resumed["total_observations"], 2)
        self.assertEqual(len(second.posts), 3)

        third = FakeClient(self.session)
        completed = import_stream(
            third,
            self.path,
            self.session["id"],
            checkpoint_path=checkpoint,
            resume=True,
        )
        self.assertTrue(completed["complete"])
        self.assertEqual(completed["batches"], 0)
        self.assertEqual(third.posts, [])

    def test_dry_run_writes_no_checkpoint(self) -> None:
        checkpoint = Path(self.temporary.name) / "dry.json"
        client = FakeClient(self.session)
        result = import_stream(
            client,
            self.path,
            self.session["id"],
            checkpoint_path=checkpoint,
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["batches"], 4)
        self.assertEqual(client.posts, [])
        self.assertFalse(checkpoint.exists())

    def test_checkpoint_hash_and_session_mismatch_are_rejected(self) -> None:
        checkpoint = Path(self.temporary.name) / "resume.json"
        import_stream(FakeClient(self.session), self.path, self.session["id"], checkpoint_path=checkpoint)
        write_stream(self.path, self.session, sweep_count=6, observation_count=2)
        with self.assertRaises(ArchiveIngestError) as caught:
            import_stream(
                FakeClient(self.session),
                self.path,
                self.session["id"],
                checkpoint_path=checkpoint,
                resume=True,
            )
        self.assertEqual(caught.exception.code, "checkpoint_mismatch")

    def test_checkpoint_requires_explicit_resume(self) -> None:
        checkpoint = Path(self.temporary.name) / "resume.json"
        import_stream(FakeClient(self.session), self.path, self.session["id"], checkpoint_path=checkpoint)
        with self.assertRaises(ArchiveIngestError) as caught:
            import_stream(FakeClient(self.session), self.path, self.session["id"], checkpoint_path=checkpoint)
        self.assertEqual(caught.exception.code, "resume_required")

    def test_stream_uses_two_passes_and_batches_over_hundred(self) -> None:
        write_stream(self.path, self.session, sweep_count=205, observation_count=0)
        client = FakeClient(self.session, limit=2)
        original_open = Path.open
        calls = 0

        def counted_open(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return original_open(self, *args, **kwargs)

        with patch.object(Path, "open", counted_open):
            result = import_stream(client, self.path, self.session["id"])
        self.assertEqual(result["sweeps"], 205)
        self.assertEqual(result["batches"], 103)
        self.assertEqual(len(client.posts), 103)
        self.assertEqual(calls, 2)

    def test_real_http_sqlite_resume_and_restart(self) -> None:
        node = HttpNode(str(Path(self.temporary.name) / "node.sqlite"))
        try:
            session = node.service.create_session(session_payload())
            stream_path = Path(self.temporary.name) / "http.ndjson"
            checkpoint = Path(self.temporary.name) / "http-resume.json"
            write_stream(stream_path, {
                "id": session.id,
                "streams": [
                    {"id": session.streams[0].id},
                    {"id": session.streams[1].id},
                ],
            }, sweep_count=3, observation_count=2)
            client = ArchiveIngestClient(node.url)
            result = import_stream(
                client,
                stream_path,
                session.id,
                batch_size=2,
                checkpoint_path=checkpoint,
            )
            self.assertEqual(result["batches"], 3)
            node.stop()
            node.start()
            resumed = import_stream(
                ArchiveIngestClient(node.url),
                stream_path,
                session.id,
                checkpoint_path=checkpoint,
                resume=True,
            )
            self.assertTrue(resumed["complete"])
            self.assertEqual(resumed["batches"], 0)
            document = build_archive_document(node.service, session.id)
            self.assertEqual(len(document["sweeps"]), 3)
            self.assertEqual(len(document["observations"]), 2)
            self.assertEqual(document["observations"][0]["level_unit"], "s_meter_count")
        finally:
            node.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
