"""Plain Python launch must not bypass the Rust-compatible writer lease."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radioinspector.runtime_bridge import RuntimeBridgeError
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from radioinspector.writer_ownership import archive_writer_owner


class UnmanagedServerOwnershipTests(unittest.TestCase):
    def test_plain_server_refuses_an_existing_owner_before_opening_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=str(Path(temporary).resolve()/'sessions.sqlite3')
            with archive_writer_owner(path), patch('radioinspector.web.server.SQLiteSessionStore',side_effect=AssertionError('SQLite opened before exclusion')):
                with self.assertRaises(RuntimeBridgeError) as rejected:
                    create_server(host='127.0.0.1',port=0,session_database=path)
                self.assertEqual(rejected.exception.code,'runtime_writer_busy')

    def test_normal_close_releases_after_archive_and_failure_retains_until_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=str(Path(temporary).resolve()/'sessions.sqlite3')
            server=create_server(host='127.0.0.1',port=0,session_database=path)
            try:
                with self.assertRaises(RuntimeBridgeError):archive_writer_owner(path)
            finally:server.server_close()
            with archive_writer_owner(path):pass

            real_close=SQLiteSessionStore.close
            with patch.object(SQLiteSessionStore,'close',side_effect=OSError('synthetic archive close failure')):
                failed=create_server(host='127.0.0.1',port=0,session_database=path)
            try:
                with self.assertRaises(OSError):failed.server_close()
                with self.assertRaises(RuntimeBridgeError):archive_writer_owner(path)
            finally:
                real_close(failed.session_service._store)
                failed.archive_owner.release_after_archive_closed()
            with archive_writer_owner(path):pass
