"""A disk-backed offered-input ledger, separate from every canonical archive."""
from __future__ import annotations

import sqlite3
import threading
import time

from .source import identifier


class Ledger:
    def __init__(self,path,*,existing=False):
        if existing:
            check=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
            try:
                if check.execute('PRAGMA application_id').fetchone()[0]!=0x4f524942 \
                        or check.execute('PRAGMA user_version').fetchone()[0]!=1 \
                        or {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}!={'offers','rendered'}:
                    raise ValueError('Not an owned benchmark ledger')
            finally:check.close()
        self.db=sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,check_same_thread=False) \
            if existing else sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        if existing:
            self.lock=threading.RLock();return
        self.db.executescript('''PRAGMA user_version=1; PRAGMA application_id=1330792770;
            CREATE TABLE offers(id TEXT PRIMARY KEY,kind TEXT,idx INTEGER,
            offered_ns INTEGER,offered_wall_ns INTEGER,due_ns INTEGER,status TEXT,http_status INTEGER,
            acknowledged_ns INTEGER,frames INTEGER,verified INTEGER DEFAULT 0,listed INTEGER DEFAULT 0);
            CREATE INDEX offer_kind_index ON offers(kind,idx);
            CREATE TABLE rendered(id TEXT,client TEXT,rendered_wall_ns INTEGER,PRIMARY KEY(id,client));''')
        self.lock=threading.RLock()
    def offer(self,config,kind,index,due_ns):
        now=time.monotonic_ns()
        with self.lock,self.db:
            self.db.execute('INSERT INTO offers VALUES (?,?,?,?,?,?,?,?,?,?,0,0)',
                (identifier(config,kind,index),kind,index,now,time.time_ns(),due_ns,'queued',None,None,
                 config.audio_bounds(index)[1] if kind=='audio' else 0))
        return now
    def status(self,identifier,status,http_status=None):
        with self.lock,self.db:
            self.db.execute('UPDATE offers SET status=?,http_status=?,acknowledged_ns=? WHERE id=?',
                (status,http_status,time.monotonic_ns(),identifier))
    def get(self,identifier):
        with self.lock:return self.db.execute('SELECT * FROM offers WHERE id=?',(identifier,)).fetchone()
    def offer_many(self,config,kind,indexes):
        now=time.monotonic_ns();wall=time.time_ns()
        with self.lock,self.db:
            self.db.executemany('INSERT INTO offers VALUES (?,?,?,?,?,?,?,?,?,?,0,0)',
                ((identifier(config,kind,index),kind,index,now,wall,now,'queued',None,None,0) for index in indexes))
        return now
    def rows(self,kind):
        # Verification happens after all writers stop. SQLite streams rows.
        yield from self.db.execute('SELECT * FROM offers WHERE kind=? ORDER BY idx',(kind,))
    def verified(self,identifier):
        with self.lock,self.db:
            changed=self.db.execute('UPDATE offers SET verified=1 WHERE id=? AND verified=0',(identifier,)).rowcount
            return bool(changed)
    def listed(self,identifier):
        with self.lock,self.db:
            return bool(self.db.execute('UPDATE offers SET listed=1 WHERE id=? AND listed=0',(identifier,)).rowcount)
    def counts(self):
        result={kind:dict(offered=0,acknowledged=0,rejected=0,queue_dropped=0,transport_unknown=0,
            injected_gap=0,cancelled=0,invalid_ack=0,verified=0) for kind in ('sweep','observation','audio')}
        mapping={'accepted':'acknowledged','rejected':'rejected','queue_full':'queue_dropped',
            'transport_unknown':'transport_unknown','injected_gap':'injected_gap',
            'cancelled':'cancelled','invalid_ack':'invalid_ack'}
        for kind,status,count in self.db.execute('SELECT kind,status,COUNT(*) FROM offers GROUP BY kind,status'):
            result[kind]['offered']+=count
            if status in mapping:result[kind][mapping[status]]+=count
        for kind,count in self.db.execute('SELECT kind,COUNT(*) FROM offers WHERE verified=1 GROUP BY kind'):
            result[kind]['verified']=count
        return result
    def gap_samples(self,maximum=32):
        return [dict(row) for row in self.db.execute('SELECT id,kind,idx,status,http_status,verified '
            'FROM offers WHERE verified=0 ORDER BY kind,idx LIMIT ?',(maximum,))]
    def audio_counts(self):
        row=self.db.execute("SELECT COALESCE(SUM(frames),0),COALESCE(SUM(CASE WHEN status='accepted' THEN frames ELSE 0 END),0),"
            "COALESCE(SUM(CASE WHEN verified=1 THEN frames ELSE 0 END),0) FROM offers WHERE kind='audio'").fetchone()
        return dict(offered_frames=row[0],acknowledged_frames=row[1],verified_frames=row[2])
    def close(self):
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');self.db.close()
