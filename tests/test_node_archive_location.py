import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

from radioinspector.runtime_bridge import RuntimeBridgeError
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.writer_ownership import archive_writer_owner


class NodeArchiveLocationTests(unittest.TestCase):
    def test_python_rollback_uses_the_selected_store_and_retains_both_leases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            data, storage = root / 'node', root / 'ssd'
            for directory in (data, storage):
                directory.mkdir(mode=0o700)
                (directory / '.ori-runtime').mkdir(mode=0o700)
            identifier = '0123456789abcdef' * 4
            for directory, name, fields in (
                (data, 'archive-location.json', {'directory': str(storage)}),
                (storage, 'archive-owner.json', {}),
            ):
                marker = directory / '.ori-runtime' / name
                marker.touch(mode=0o600)
                marker.write_text(json.dumps({'version': 1, 'id': identifier, **fields}))
            pointer = data / 'sessions.sqlite3'
            with self.assertRaisesRegex(RuntimeBridgeError, 'requires_stopped_supervisor'):
                archive_writer_owner(pointer)
            pointer.touch(mode=0o600)
            pointer.write_bytes(b'ORI relocated archive v1\n' + (data / '.ori-runtime/archive-location.json').read_bytes())
            target = storage / 'sessions.sqlite3'
            target.touch(mode=0o600)
            with archive_writer_owner(data / 'sessions.sqlite3') as owner:
                self.assertEqual(owner.database_path, target)
                with self.assertRaises(RuntimeBridgeError):
                    archive_writer_owner(target)
            # A stopped handoff may lose power between retiring the private
            # selection and atomically replacing the legacy name.
            (data / '.ori-runtime/archive-location.json').unlink()
            with archive_writer_owner(pointer) as owner:
                self.assertEqual(owner.database_path, target)
            with archive_writer_owner(pointer) as owner:
                target.unlink()
                self.assertTrue(owner.require_existing_archive)
                with self.assertRaises(sqlite3.OperationalError):
                    SQLiteSessionStore(str(owner.database_path), require_existing=owner.require_existing_archive)
                self.assertFalse(target.exists())
            with self.assertRaises(RuntimeBridgeError):
                archive_writer_owner(data / 'sessions.sqlite3')
            self.assertTrue(pointer.is_file())
