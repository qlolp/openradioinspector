"""Additive, versioned metadata on the core's existing SQLite writer."""
import copy
import json
from contextlib import contextmanager

from ..session_store import SQLiteSessionStore


class ProjectStore:
    VERSION = 2

    def __init__(self, store):
        self.owned = store is None
        self.store = store or SQLiteSessionStore(':memory:')
        self.lock = self.store._lock
        self.db = self.store._connection
        with self.lock:
            version = self.db.execute('SELECT version FROM monitoring_schema').fetchone()
            if not version or version[0] != self.VERSION:
                raise ValueError('Неподдерживаемая версия проектов наблюдения')

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = not self.db.in_transaction
            if outer: self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                if outer: self.db.commit()
            except BaseException:
                if outer: self.db.rollback()
                raise

    def put(self, kind, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self.transaction():
            self.db.execute('''INSERT INTO monitoring_records VALUES (?,?,?,?,?)
                ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload''',
                (kind, value['id'], value.get('project_id', value['id']), value['created_at'], payload))
            if kind=='project':
                self.db.executemany('INSERT OR IGNORE INTO monitoring_sessions VALUES (?,?)',
                    [(value['id'],sid) for sid in value['session_ids']])
        return copy.deepcopy(value)

    def get(self, kind, identifier):
        with self.lock:
            row = self.db.execute('SELECT payload FROM monitoring_records WHERE kind=? AND id=?',
                                  (kind, identifier)).fetchone()
        if row is None:
            raise KeyError('Запись наблюдения не найдена')
        return json.loads(row[0])

    def list(self, kind, project=None, *, limit=200, after='', state=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit должен быть от 1 до 1000')
        clause = ' AND project_id=?' if project is not None else ''
        args = [kind, after] + ([project] if project is not None else [])
        if state is not None:
            clause += " AND json_extract(payload,'$.state')=?"
            args.append(state)
        args.append(limit)
        with self.lock:
            rows = self.db.execute('SELECT payload FROM monitoring_records WHERE kind=? AND id>?'
                                   + clause + ' ORDER BY id LIMIT ?', args).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self):
        if self.owned:
            self.store.close()

    def pending_receiver_events(self, project, session):
        with self.lock:
            rows=self.db.execute("""SELECT payload FROM monitoring_records
                WHERE kind='event' AND project_id=?
                AND json_extract(payload,'$.analysis.state') IS NULL
                AND json_extract(payload,'$.source_refs[#-1].session_id')=?
                AND json_extract(payload,'$.disposition')='unreviewed'
                AND json_extract(payload,'$.kind') NOT IN ('broadband_change','authorized_changed')
                ORDER BY json_extract(payload,'$.last_seen') DESC,id DESC LIMIT 1""",(project,session)).fetchall()
        return [json.loads(row[0]) for row in rows]
