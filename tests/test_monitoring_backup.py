import json
import sqlite3
import tempfile
import base64
import hashlib
import unittest
from contextlib import closing
from pathlib import Path
from tests import test_monitoring_guard as guard_tests
from tests.test_monitoring_detector import sweep


class MonitoringBackupTests(unittest.TestCase):
    setUp=guard_tests.GuardTests.setUp

    def test_backup_preserves_original_sweeps_and_strips_automatic_actions(self):
        from radioinspector.monitoring.archive_backup import backup_archive
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'],dict(name='Quiet',location='Fixture',confirm=True))
        self.service.update_settings(self.project['id'],dict(autostart=True,webhook=dict(enabled=True,url='http://127.0.0.1/hook',secret='private-backup-secret')))
        project=self.service.project(self.project['id']);project.update(state='armed',resume_requested=True)
        self.service.store.put('project',project)
        raw=b'RIFF\x04\x00\x00\x00WAVE'
        artifact=self.sessions.save_instrument_artifact(self.session.id,self.session.streams[1].id,dict(
            kind='audio',mime_type='audio/wav',filename='synthetic.wav',data_base64=base64.b64encode(raw).decode(),
            sha256=hashlib.sha256(raw).hexdigest(),metadata={'occurred_at':'2026-01-01T00:00:00Z','synthetic':True}))
        original=self.sessions._store._connection.execute('SELECT * FROM sweeps ORDER BY id').fetchall()
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'portable.sqlite';backup_archive(self.sessions._store.path,target)
            with closing(sqlite3.connect(target)) as db:
                copied=db.execute('SELECT * FROM sweeps ORDER BY id').fetchall()
                self.assertEqual(copied,[tuple(x) for x in original])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM monitoring_webhooks').fetchone()[0],0)
                payload=json.loads(db.execute("SELECT payload FROM monitoring_records WHERE kind='project'").fetchone()[0])
                self.assertFalse(payload['autostart']);self.assertFalse(payload['webhook_enabled']);self.assertFalse(payload['resume_requested'])
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('SELECT payload FROM instrument_artifacts WHERE id=?',(artifact['id'],)).fetchone()[0],raw)
            self.assertNotIn(b'private-backup-secret',target.read_bytes())
            with self.assertRaises(FileExistsError):backup_archive(self.sessions._store.path,target)
        self.assertEqual(self.service.project(self.project['id']),project)

    def test_source_path_cannot_be_overwritten(self):
        from radioinspector.monitoring.archive_backup import backup_archive
        with self.assertRaises(ValueError):backup_archive(self.sessions._store.path,self.sessions._store.path)
