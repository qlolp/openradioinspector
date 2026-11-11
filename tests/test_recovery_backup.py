"""Recovery snapshots include WAL commits and exact closed audio artifacts."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests import test_monitoring_guard as guards
from tests.test_monitoring_detector import sweep
from tests.archive_migration_fixture import add_current_metadata, current_metadata_values


class RecoveryBackupTests(unittest.TestCase):
    setUp = guards.GuardTests.setUp

    def functions(self):
        self.assertIsNotNone(importlib.util.find_spec('radioinspector.recovery_backup'),
                             'Verified recovery backup/restore is missing')
        from radioinspector.recovery_backup import create_backup, verify_backup, restore_backup
        return create_backup, verify_backup, restore_backup

    def test_live_wal_snapshot_restores_sources_audio_and_recovery_intent(self):
        create, verify, restore = self.functions()
        for index in range(3):
            self.sessions.ingest_sweep(sweep(index, session=self.session.id, stream=self.session.streams[0].id))
        add_current_metadata(self.sessions, self.session.id, self.session.streams[1].id)
        project = self.service.project(self.project['id'])
        project.update(autostart=True, resume_requested=True, state='armed')
        self.service.store.put('project', project)
        expected = current_metadata_values(self.sessions._store._connection)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backup = directory / 'snapshot'
            manifest = create(self.sessions._store.path, backup)
            self.assertEqual(manifest['counts']['sweeps'], 3)
            self.assertEqual(manifest['counts']['artifacts'], 1)
            self.assertEqual(manifest, verify(backup))
            self.assertEqual(self.service.project(self.project['id']), project)
            self.sessions.ingest_sweep(sweep(3, session=self.session.id, stream=self.session.streams[0].id))
            target = directory / 'restored.sqlite'
            restore(backup, target)
            db = sqlite3.connect(target)
            try:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM sweeps').fetchone()[0], 3)
                self.assertEqual(current_metadata_values(db), expected)
            finally:
                db.close()
            before = target.read_bytes()
            with self.assertRaises(FileExistsError):
                restore(backup, target)
            self.assertEqual(target.read_bytes(), before)
            with self.assertRaises(FileExistsError):
                create(self.sessions._store.path, backup)

    def test_corrupted_backup_is_rejected_before_creating_restore_target(self):
        create, verify, restore = self.functions()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backup = directory / 'snapshot'
            create(self.sessions._store.path, backup)
            with (backup / 'archive.sqlite').open('r+b') as output:
                output.seek(100); output.write(b'broken')
            with self.assertRaises(ValueError):
                verify(backup)
            target = directory / 'restored.sqlite'
            with self.assertRaises(ValueError):
                restore(backup, target)
            self.assertFalse(target.exists())

    def test_invalid_closed_audio_checksum_aborts_snapshot(self):
        create, _, _ = self.functions()
        artifact = add_current_metadata(self.sessions, self.session.id, self.session.streams[1].id)
        with self.sessions._store._connection:
            self.sessions._store._connection.execute('UPDATE instrument_artifacts SET payload=? WHERE id=?',
                                                      (b'corrupted-audio', artifact['id']))
        with tempfile.TemporaryDirectory() as temporary:
            backup = Path(temporary) / 'snapshot'
            with self.assertRaisesRegex(ValueError, 'checksum'):
                create(self.sessions._store.path, backup)
            self.assertFalse(backup.exists())

    def test_manifest_cannot_select_a_path_outside_the_bundle(self):
        create, verify, _ = self.functions()
        with tempfile.TemporaryDirectory() as temporary:
            backup = Path(temporary) / 'snapshot'
            create(self.sessions._store.path, backup)
            manifest = json.loads((backup/'manifest.json').read_text(encoding='utf-8'))
            manifest['archive']['file'] = '../outside.sqlite'
            (backup/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaises(ValueError):
                verify(backup)

    def stale_wal(self, directory):
        path = directory / 'unrelated.sqlite'
        db = sqlite3.connect(path)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('CREATE TABLE unrelated(value TEXT)')
            db.execute("INSERT INTO unrelated VALUES ('stale-wal')")
            db.commit()
            value = Path(str(path)+'-wal').read_bytes()
            self.assertGreater(len(value), 32)
            return value
        finally:
            db.close()

    def test_restore_rejects_real_stale_wal_and_other_sidecars(self):
        create, _, restore = self.functions()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backup = directory / 'snapshot'
            create(self.sessions._store.path, backup)
            wal = self.stale_wal(directory)
            for suffix in ('-wal', '-shm', '-journal'):
                with self.subTest(suffix=suffix):
                    target = directory / f'restored{suffix}.sqlite'
                    sidecar = Path(str(target)+suffix)
                    sidecar.write_bytes(wal if suffix == '-wal' else b'owned-sidecar')
                    before = sidecar.read_bytes()
                    with self.assertRaises(FileExistsError):
                        restore(backup, target)
                    self.assertFalse(target.exists())
                    self.assertEqual(sidecar.read_bytes(), before)
                    sidecar.unlink()
            if os.name == 'posix':
                target = directory / 'linked.sqlite'
                sidecar = Path(str(target)+'-wal')
                sidecar.symlink_to(directory/'absent')
                with self.assertRaises(FileExistsError):
                    restore(backup, target)
                self.assertTrue(sidecar.is_symlink())
                self.assertFalse(target.exists())

    def test_sidecar_created_during_verification_prevents_publication(self):
        create, verify, restore = self.functions()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            backup = directory / 'snapshot'
            create(self.sessions._store.path, backup)
            target = directory / 'restored.sqlite'
            wal = self.stale_wal(directory)
            def concurrent_sidecar(value):
                result = verify(value)
                Path(str(target)+'-wal').write_bytes(wal)
                return result
            with patch('radioinspector.recovery_backup.verify_backup', concurrent_sidecar):
                with self.assertRaises(FileExistsError):
                    restore(backup, target)
            self.assertFalse(target.exists())

    def test_file_sync_uses_writable_handles_required_by_windows(self):
        create, _, _ = self.functions()
        sync = os.fsync
        def writable_sync(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.write(descriptor, b'')
            return sync(descriptor)
        with tempfile.TemporaryDirectory() as temporary:
            with patch('radioinspector.recovery_backup.os.fsync', writable_sync):
                try:
                    manifest = create(self.sessions._store.path, Path(temporary)/'snapshot')
                except OSError as error:
                    self.fail(f'Backup fsync must work with Windows writable-handle semantics: {error}')
            self.assertEqual(manifest['counts']['sweeps'], 0)

    def test_literal_home_relative_backup_path_restores_the_verified_file(self):
        create, _, restore = self.functions()
        with tempfile.TemporaryDirectory(dir=Path.home()) as temporary:
            directory = Path(temporary)
            backup = directory / 'snapshot'
            create(self.sessions._store.path, backup)
            argument = str(Path('~') / backup.relative_to(Path.home()))
            try:
                restore(argument, directory/'restored.sqlite')
            except FileNotFoundError as error:
                self.fail(f'Restore must open the same expanded path it verified: {error}')
            self.assertTrue((directory/'restored.sqlite').is_file())
