"""SQLite REAL alone loses negative zero; canonical observations must not."""
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.archive_export_stream import iter_archive_ndjson
from radioinspector.recovery_backup import create_backup, restore_backup


class ObservationPrecisionTests(unittest.TestCase):
    def fixture(self):
        return json.loads((Path(__file__).resolve().parents[1]/'contracts/fixtures/measurement-v1.json').read_text(encoding='utf-8'))

    def test_streaming_export_and_private_restore_keep_original_bits(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root/'archive.sqlite'
            service = SessionService(store=SQLiteSessionStore(str(path)))
            try:
                service.create_session(fixture['session'])
                service.ingest_observation(dict(fixture['observation'], level=-0.0))
                lines = [json.loads(line) for line in iter_archive_ndjson(service, fixture['session']['id'])]
                value = next(line['measurement']['level'] for line in lines if line.get('kind') == 'observation')
                self.assertEqual(struct.pack('<d', value), struct.pack('<d', -0.0))
            finally:
                service.close()
            create_backup(path, root/'backup')
            restore_backup(root/'backup', root/'restored.sqlite')
            restored = SessionService(store=SQLiteSessionStore(str(root/'restored.sqlite')))
            try:
                value = restored.list_observations(fixture['session']['id'])[0].level
                self.assertEqual(struct.pack('<d', value), struct.pack('<d', -0.0))
            finally:
                restored.close()

    def test_future_extension_is_rejected_without_rewriting_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'archive.sqlite'
            SQLiteSessionStore(str(path)).close()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('UPDATE ori_archive_schema SET version=2')
                db.execute('PRAGMA journal_mode=DELETE')
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'extension'):
                SQLiteSessionStore(str(path))
            self.assertEqual(path.read_bytes(), before)

    def test_missing_legacy_bits_are_not_guessed_and_corruption_is_explicit(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary)/'archive.sqlite')
            service = SessionService(store=SQLiteSessionStore(path))
            try:
                service.create_session(fixture['session'])
                service.ingest_observation(dict(fixture['observation'], level=-0.0))
                db = service._store._connection
                with db:
                    db.execute('DELETE FROM ori_observation_level_bits')
                self.assertEqual(struct.pack('<d', service.list_observations(fixture['session']['id'])[0].level), struct.pack('<d', 0.0))
                with db:
                    db.execute('INSERT INTO ori_observation_level_bits VALUES (?,?)',
                               (fixture['observation']['id'], struct.pack('<d', 5.0)))
                with self.assertRaisesRegex(ValueError, 'disagree'):
                    service.list_observations(fixture['session']['id'])
            finally:
                service.close()

    def test_rejected_batch_rolls_back_canonical_bits_and_events_together(self):
        fixture=self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            store=SQLiteSessionStore(str(Path(temporary)/'archive.sqlite'))
            service=SessionService(store=store)
            try:
                service.create_session(fixture['session'])
                before=service.events()
                with self.assertRaises(ValueError):
                    service.ingest_observations([dict(fixture['observation'],level=-0.0),dict(fixture['observation'],level=2.)])
                self.assertEqual(service.events(),before)
                self.assertEqual(store._connection.execute('SELECT COUNT(*) FROM ori_observation_level_bits').fetchone()[0],0)
            finally:service.close()

    def test_all_finite_original_level_bits_survive_reopen(self):
        fixture = json.loads((Path(__file__).resolve().parents[1]/'contracts/fixtures/measurement-v1.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary)/'archive.sqlite')
            service = SessionService(store=SQLiteSessionStore(path))
            levels = (-0.0, 1.0000000000000002, 137.0, None)
            try:
                service.create_session(fixture['session'])
                for index, level in enumerate(levels):
                    service.ingest_observation(dict(fixture['observation'], id=f'level-{index}', level=level))
            finally:
                service.close()
            restored = SessionService(store=SQLiteSessionStore(path))
            try:
                values = {item.id: item.level for item in restored.list_observations(fixture['session']['id'])}
                for index, level in enumerate(levels):
                    with self.subTest(level=level):
                        if level is None:
                            self.assertIsNone(values[f'level-{index}'])
                        else:
                            self.assertEqual(struct.pack('<d', values[f'level-{index}']), struct.pack('<d', level))
            finally:
                restored.close()
