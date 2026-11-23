import json
from pathlib import Path
import tempfile
import unittest

from radioinspector.runtime_bridge import RuntimeBridgeError
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
            target = storage / 'sessions.sqlite3'
            target.touch(mode=0o600)
            with archive_writer_owner(data / 'sessions.sqlite3') as owner:
                self.assertEqual(owner.database_path, target)
                with self.assertRaises(RuntimeBridgeError):
                    archive_writer_owner(target)
            target.unlink()
            with self.assertRaises(RuntimeBridgeError):
                archive_writer_owner(data / 'sessions.sqlite3')
            self.assertFalse((data / 'sessions.sqlite3').exists())
