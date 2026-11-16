"""Streaming logical verification of all original live sources and their restore."""
import hashlib
import os
from pathlib import Path
import sqlite3
from ..client import LINE_LIMIT,segment
from ..config import BenchmarkError
from ..source import measurement_hash
from tools.modernization.api_parity import load_json


class SourceProof:
    def __init__(self,path):
        path=Path(path)
        if path.exists():raise BenchmarkError('existing_evidence','Source ledger already exists')
        self.db=sqlite3.connect(path);os.chmod(path,0o600)
        self.db.executescript('''PRAGMA synchronous=FULL;
          CREATE TABLE sources(session BLOB,kind TEXT,id BLOB,hash TEXT,restored INTEGER DEFAULT 0,committed INTEGER DEFAULT 0,
          PRIMARY KEY(session,kind,id)) WITHOUT ROWID;''')
    @staticmethod
    def fields(row):
        try:
            kind,item=row['kind'],row['measurement']
            if kind not in ('sweep','observation'):raise ValueError()
            return item['session_id'].encode('utf-8'),kind,item['id'].encode('utf-8'),measurement_hash(kind,item)
        except (ValueError,KeyError,TypeError,OverflowError) as e:
            raise BenchmarkError('source_invalid','Original source identity/values invalid') from e
    def original(self,row):
        try:self.db.execute('INSERT INTO sources(session,kind,id,hash) VALUES (?,?,?,?)',self.fields(row))
        except sqlite3.IntegrityError as e:raise BenchmarkError('source_duplicate','Duplicate original ID') from e
    def restored(self,row):
        session,kind,identifier,digest=self.fields(row)
        changed=self.db.execute('UPDATE sources SET restored=1 WHERE session=? AND kind=? AND id=? AND hash=? AND restored=0',
                                (session,kind,identifier,digest)).rowcount
        if changed!=1:raise BenchmarkError('source_changed','Restored source is changed, foreign or duplicated')
    def committed(self,row):
        try:
            changed=self.db.execute('UPDATE sources SET committed=1 WHERE session=? AND kind=? AND id=? AND committed=0',
                (row['session_id'].encode('utf-8'),row['kind'],row['source_id'].encode('utf-8'))).rowcount
        except (KeyError,AttributeError,TypeError) as error:raise BenchmarkError('source_commit_invalid','Commit receipt lacks source identity') from error
        if changed!=1:raise BenchmarkError('source_commit_invalid','Commit source is missing, foreign or duplicated')
    def finish(self,*,require_commits=False):
        hashes=[hashlib.sha256(),hashlib.sha256()];counts={'sweep':0,'observation':0};missing=0;missing_commits=0
        for session,kind,identifier,digest,restored,committed in self.db.execute('SELECT * FROM sources ORDER BY session,kind,id'):
            counts[kind]+=1
            for number in range(2 if restored else 1):
                for value in (session,kind.encode(),identifier,bytes.fromhex(digest)):
                    hashes[number].update(len(value).to_bytes(8,'big'));hashes[number].update(value)
            if not restored:missing+=1
            if not committed:missing_commits+=1
        self.db.commit()
        return dict(status='passed' if not missing and all(counts.values()) and (not require_commits or not missing_commits) else 'failed',
                    sweeps=counts['sweep'],observations=counts['observation'],missing=missing,
                    commits_verified=not missing_commits,missing_commits=missing_commits,
                    original_sha256=hashes[0].hexdigest(),restored_sha256=hashes[1].hexdigest())
    def close(self):self.db.commit();self.db.close()


def export(client,session_id,consume):
    header=True
    with client.open('/api/v1/sessions/'+segment(session_id)+'/archive/export.ndjson',timeout=600) as response:
        while raw:=response.readline(LINE_LIMIT+1):
            if len(raw)>LINE_LIMIT or not raw.endswith(b'\n'):
                raise BenchmarkError('source_invalid','Incomplete or oversized archive line')
            value=load_json(raw,LINE_LIMIT)
            if header:
                if value!={'format':'openradioinspector-archive-ndjson','format_version':1,'session_id':session_id}:
                    raise BenchmarkError('source_invalid','Unexpected original archive header')
                header=False
            else:
                if value.get('measurement',{}).get('session_id')!=session_id:
                    raise BenchmarkError('source_invalid','Foreign session in archive')
                consume(value)
    if header:raise BenchmarkError('source_invalid','Empty archive response')
