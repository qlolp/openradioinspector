"""Атомарность снимка двух типов архива при конкурентной записи."""

from __future__ import annotations

import threading
import unittest

from radioinspector.archive_export import build_archive_document
from radioinspector.session_service import SessionService
from tests.test_persistence import observation_payload, sweep_payload
from tests.test_platform import session_payload


class BlockingListService(SessionService):
    def __init__(self) -> None:
        super().__init__()
        self.export_started = threading.Event()
        self.release_export = threading.Event()

    def list_sweeps(self, *args, **kwargs):
        result = super().list_sweeps(*args, **kwargs)
        self.export_started.set()
        if not self.release_export.wait(timeout=3):
            raise TimeoutError("Тест не разрешил продолжить экспорт")
        return result


class ArchiveSnapshotTests(unittest.TestCase):
    def test_writer_cannot_interleave_export_types(self) -> None:
        service = BlockingListService()
        session = service.create_session(session_payload())
        spectrum_id, receiver_id = (item.id for item in session.streams)
        service.ingest_sweep(sweep_payload(session.id, spectrum_id))
        exported = []
        errors = []

        def export() -> None:
            try:
                exported.append(build_archive_document(service, session.id))
            except Exception as exc:  # noqa: BLE001 - выводится assertion ниже
                errors.append(exc)

        export_thread = threading.Thread(target=export)
        export_thread.start()
        self.assertTrue(service.export_started.wait(timeout=2))

        writer_finished = threading.Event()

        def write_observation() -> None:
            try:
                service.ingest_observation(
                    observation_payload(session.id, receiver_id)
                )
            finally:
                writer_finished.set()

        writer = threading.Thread(target=write_observation)
        writer.start()
        self.assertFalse(
            writer_finished.wait(timeout=0.1),
            "Writer вклинился между чтением sweeps и observations",
        )
        service.release_export.set()
        export_thread.join(timeout=4)
        writer.join(timeout=4)
        self.assertFalse(export_thread.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(writer_finished.is_set())

        self.assertEqual(len(exported), 1)
        self.assertEqual(len(exported[0]["sweeps"]), 1)
        self.assertEqual(exported[0]["observations"], [])
        self.assertEqual(service.health()["last_event_id"], 3)

        following = build_archive_document(service, session.id)
        self.assertEqual(len(following["observations"]), 1)
        self.assertEqual(
            following["observations"][0]["level_unit"], "s_meter_count"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
