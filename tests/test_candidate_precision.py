"""Candidate scalar supplements preserve new values without guessing history."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.recovery_backup import create_backup,restore_backup
from tests.test_platform import session_payload


class CandidatePrecisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve()
        self.path=str(self.root/'archive.sqlite')
        self.service=SessionService(store=SQLiteSessionStore(self.path))
        self.addCleanup(lambda:self.service.close())
        self.session=self.service.create_session(session_payload())

    def candidate(self,**changes):
        body=dict(id='candidate-𐀀',source_stream_id=self.session.streams[0].id,
            center_frequency_hz=100000001,bandwidth_hz=None,peak_level=-0.0,score=-0.0,
            level_unit='dBm',observation_ids=[],
            interval={'started_at':'2026-11-11T00:00:00.123456Z',
                      'ended_at':'2026-11-11T00:00:00.223456Z'})
        return self.service.create_candidate(self.session.id,dict(body,**changes))

    def exact(self,value,expected):
        self.assertEqual(struct.pack('<d',value),struct.pack('<d',expected))

    def test_list_get_reopen_and_private_restore_preserve_original_scalar_bits(self):
        original=self.candidate(score=1.0000000000000002)
        self.exact(self.service.list_candidates(self.session.id)[0].peak_level,-0.0)
        self.service.close()
        create_backup(self.path,self.root/'backup')
        restore_backup(self.root/'backup',self.root/'restored.sqlite')
        for path in (self.path,str(self.root/'restored.sqlite')):
            store=SQLiteSessionStore(path)
            try:
                actual=store.get_candidate(original.id)
                self.exact(actual.peak_level,original.peak_level)
                self.exact(actual.score,original.score)
            finally:store.close()

    def test_nullable_update_clears_bits_and_old_rows_use_actual_stored_real(self):
        original=self.candidate()
        store=self.service._store
        with store._connection:
            store._connection.execute('DELETE FROM ori_candidate_scalar_bits')
        self.exact(store.get_candidate(original.id).peak_level,0.0)
        store.save_candidate(replace(original,peak_level=None,score=None))
        self.assertIsNone(store.get_candidate(original.id).peak_level)
        self.assertIsNone(store.get_candidate(original.id).score)
        self.assertEqual(store._connection.execute('SELECT COUNT(*) FROM ori_candidate_scalar_bits').fetchone()[0],0)

    def test_rejected_event_rolls_back_candidate_and_scalar_supplement(self):
        db=self.service._store._connection
        with db:db.execute("""CREATE TRIGGER reject_candidate_event BEFORE INSERT ON events
            WHEN NEW.event_type='candidate.created'
            BEGIN SELECT RAISE(ABORT,'synthetic event write failure'); END""")
        before=self.service.events()
        with self.assertRaises(ValueError):self.candidate()
        self.assertEqual(self.service.events(),before)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM ori_candidate_scalar_bits').fetchone()[0],0)

    def test_corrupt_scalar_bits_are_explicit_instead_of_silently_recalibrated(self):
        original=self.candidate()
        db=self.service._store._connection
        for bits in (struct.pack('<d',5.0),struct.pack('<d',float('nan'))):
            with db:db.execute('UPDATE ori_candidate_scalar_bits SET peak_level_bits_le=? WHERE candidate_id=?',(bits,original.id))
            with self.assertRaisesRegex(ValueError,'[Cc]andidate.*(bytes|REAL)'):
                self.service._store.get_candidate(original.id)

    def test_future_extension_refuses_before_any_archive_write(self):
        self.service.close()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS ori_candidate_precision_schema(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL)')
            db.execute('INSERT OR REPLACE INTO ori_candidate_precision_schema VALUES(1,2)')
            db.execute('CREATE TABLE IF NOT EXISTS ori_candidate_scalar_bits(candidate_id TEXT PRIMARY KEY,peak_level_bits_le BLOB,score_bits_le BLOB)')
            db.execute('PRAGMA journal_mode=DELETE')
        before=Path(self.path).read_bytes()
        with self.assertRaisesRegex(ValueError,'[Cc]andidate.*extension'):
            SQLiteSessionStore(self.path)
        self.assertEqual(Path(self.path).read_bytes(),before)

    def test_partial_extension_is_rejected_before_any_archive_write(self):
        self.service.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE ori_candidate_scalar_bits')
            db.execute('PRAGMA journal_mode=DELETE')
        before=Path(self.path).read_bytes()
        with self.assertRaisesRegex(ValueError,'[Cc]andidate.*extension'):
            SQLiteSessionStore(self.path)
        self.assertEqual(Path(self.path).read_bytes(),before)

    def test_private_backup_refuses_corrupt_candidate_bytes_without_a_success_manifest(self):
        original=self.candidate()
        with self.service._store._connection as db:
            db.execute('UPDATE ori_candidate_scalar_bits SET score_bits_le=? WHERE candidate_id=?',
                       (struct.pack('<d',5.0),original.id))
        with self.assertRaisesRegex(ValueError,'[Cc]andidate.*(bytes|REAL)'):
            create_backup(self.path,self.root/'corrupt-backup')
        self.assertFalse((self.root/'corrupt-backup'/'manifest.json').exists())
