"""A successful measurement acknowledgement uses durable WAL commits."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_platform import session_payload
from tests.test_monitoring_detector import sweep


class ArchiveDurabilityTests(unittest.TestCase):
    def test_persistent_writer_uses_full_wal_sync_on_every_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / 'archive.sqlite')
            for _ in range(2):
                store = SQLiteSessionStore(path)
                try:
                    self.assertEqual(store._connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    self.assertEqual(store._connection.execute('PRAGMA synchronous').fetchone()[0], 2,
                                     'Acknowledged WAL commits must sync before publication')
                finally:
                    store.close()

    def test_rejected_sqlite_write_is_not_published_or_left_in_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteSessionStore(str(Path(temporary) / 'archive.sqlite'))
            service = SessionService(store=store)
            try:
                session = service.create_session(session_payload())
                source = sweep(0, session=session.id, stream=session.streams[0].id)
                before = service.events()
                with store._connection:
                    store._connection.execute("""CREATE TRIGGER reject_source BEFORE INSERT ON sweeps
                        BEGIN SELECT RAISE(ABORT, 'synthetic disk write failure'); END""")
                with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                    service.ingest_sweep(source)
                self.assertEqual(service.events(), before)
                self.assertEqual(service.list_sweeps(session.id), [])
            finally:
                service.close()
