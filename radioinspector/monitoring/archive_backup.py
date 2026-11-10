"""Consistent portable SQLite backup; raw rows preserved, automatic actions disabled."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


def backup_archive(source,destination):
    source=Path(source).expanduser().resolve();destination=Path(destination).expanduser().absolute()
    if destination.resolve()==source:raise ValueError('Нельзя перезаписать исходный архив')
    if destination.exists() or destination.is_symlink():raise FileExistsError(destination)
    if not source.is_file():raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ori-export-',dir=destination.parent) as temporary:
        staging=Path(temporary)/'archive.sqlite'
        original=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
        copied=sqlite3.connect(staging)
        try:
            original.backup(copied,pages=128)
            copied.execute('PRAGMA secure_delete=ON')
            with copied:
                if copied.execute("SELECT 1 FROM sqlite_master WHERE name='monitoring_webhooks'").fetchone():
                    copied.execute('DELETE FROM monitoring_webhooks')
                if copied.execute("SELECT 1 FROM sqlite_master WHERE name='monitoring_records'").fetchone():
                    cursor=copied.execute("SELECT id,payload FROM monitoring_records WHERE kind='project'")
                    while True:
                        rows=cursor.fetchmany(100)
                        if not rows:break
                        for identifier,raw in rows:
                            project=json.loads(raw);project.update(autostart=False,resume_requested=False,webhook_enabled=False,coverage='stopped')
                            if project['state'] in ('armed','searching','degraded','collecting'):project['state']='interrupted'
                            if project.get('preparation'):
                                project['preparation'].update(receive_path_confirmed=False,location_confirmed=False)
                            copied.execute("UPDATE monitoring_records SET payload=? WHERE kind='project' AND id=?",
                                (json.dumps(project,ensure_ascii=False,allow_nan=False),identifier))
                if copied.execute("SELECT 1 FROM sqlite_master WHERE name='monitoring_notifications'").fetchone():
                    copied.execute("UPDATE monitoring_notifications SET state='disabled',lease_until=NULL,next_at=NULL WHERE state IN ('pending','delivering')")
            # Rebuild free pages as well: historical credential values must not
            # survive as recoverable deleted content in the portable file.
            copied.execute('VACUUM')
            if copied.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('Проверка целостности копии не пройдена')
            if copied.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('Нарушены ссылки в копии архива')
        finally:copied.close();original.close()
        os.chmod(staging,0o600)
        try:os.link(staging,destination)
        except FileExistsError:raise
        except OSError:
            # FAT/removable filesystems may not support hard links. The private
            # O_EXCL descriptor cannot follow or overwrite an existing path.
            descriptor=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            try:
                with os.fdopen(descriptor,'wb') as target,staging.open('rb') as input_file:
                    shutil.copyfileobj(input_file,target,1024*1024);target.flush();os.fsync(target.fileno())
            except BaseException:
                destination.unlink(missing_ok=True);raise
    return dict(path=str(destination),bytes=destination.stat().st_size,automatic_actions_enabled=False)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    print(json.dumps(backup_archive(args.source,args.output),ensure_ascii=False))


if __name__=='__main__':main()
