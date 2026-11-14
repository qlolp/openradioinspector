"""Disk-backed derived event evidence; never a writer of canonical records."""
from __future__ import annotations

import hashlib
import sqlite3

from .config import BenchmarkError
from .source import encode


def invalid():
    return BenchmarkError('events_changed','Original event sequence, identity or payload changed')


class EventProof:
    def __init__(self,path):
        if path.exists() or path.is_symlink():
            raise BenchmarkError('unsafe_output','Event proof must be a new owned file')
        self.db = sqlite3.connect(path)
        self.db.executescript('PRAGMA application_id=1330792773; PRAGMA user_version=1; '
                             'CREATE TABLE events(sequence INTEGER PRIMARY KEY,digest TEXT,payload TEXT)')
        self.begin(record=True)

    def begin(self,*,record=False):
        if record and self.db.execute('SELECT 1 FROM events LIMIT 1').fetchone():
            raise invalid()
        self.record,self.cursor,self.digest = record,0,hashlib.sha256()
        self.types = {}

    def observe(self,event):
        if not isinstance(event,dict) or type(event.get('sequence')) is not int \
                or event['sequence']!=self.cursor+1 or not isinstance(event.get('event_type'),str) \
                or not event['event_type']:
            raise invalid()
        try:
            raw = encode(event)
        except (ValueError,TypeError,OverflowError,RecursionError):
            raise invalid() from None
        if len(raw)>2*1024*1024:
            raise BenchmarkError('events_too_large','Event exceeds the bounded proof record size')
        digest = hashlib.sha256(raw).hexdigest()
        if self.record:
            self.db.execute('INSERT INTO events VALUES (?,?,?)',
                            (event['sequence'],digest,raw.decode('utf-8')))
        elif self.db.execute('SELECT digest FROM events WHERE sequence=?',
                             (event['sequence'],)).fetchone()!=(digest,):
            raise invalid()
        self.cursor = event['sequence']
        self.digest.update(raw+b'\n')
        kind = event['event_type']
        self.types[kind] = self.types.get(kind,0)+1
        if len(self.types)>32:raise invalid()

    def finish(self,watermark):
        if type(watermark) is not int or watermark!=self.cursor \
                or self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]!=watermark:
            raise invalid()
        self.db.commit()
        return dict(count=self.cursor,last_event_id=watermark,sha256=self.digest.hexdigest(),types=self.types)

    def close(self):
        self.db.close()


def checkpoint(client,proof,*,record=False,expected_watermark=None):
    watermark = client.json('/api/v1/health')['last_event_id']
    if expected_watermark is not None and watermark!=expected_watermark:
        raise invalid()
    proof.begin(record=record)
    pages,maximum = 0,0
    while proof.cursor<watermark:
        rows = client.json('/api/v1/events/history?after='+str(proof.cursor)+'&limit=8')
        pages += 1
        if not isinstance(rows,list) or not 1<=len(rows)<=8 or pages>1024:
            raise invalid()
        maximum = max(maximum,len(rows))
        for event in rows:
            proof.observe(event)
    if client.json('/api/v1/health')['last_event_id']!=watermark:
        raise invalid()
    return proof.finish(watermark)|dict(pages=pages,max_page_rows=maximum)
