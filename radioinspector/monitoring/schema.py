"""Additive project schema, installed within SQLiteSessionStore migration."""

def install_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS monitoring_schema (version INTEGER NOT NULL)')
    version = db.execute('SELECT version FROM monitoring_schema').fetchone()
    if version and version[0] > 4:
        raise ValueError('Неподдерживаемая версия проектов наблюдения')
    db.execute('''CREATE TABLE IF NOT EXISTS monitoring_records (
        kind TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,id))''')
    db.execute('''CREATE INDEX IF NOT EXISTS monitoring_scope_time
        ON monitoring_records(kind,project_id,created_at,id)''')
    if not version:
        db.execute('INSERT INTO monitoring_schema VALUES (?)', (4,))
    else:
        if version[0]<4:
            from ..contracts import parse_time
            cursor=db.execute('SELECT kind,id,created_at FROM monitoring_records')
            while True:
                rows=cursor.fetchmany(128)
                if not rows:break
                db.executemany('UPDATE monitoring_records SET created_at=? WHERE kind=? AND id=?',
                    [(parse_time(row[2]).isoformat(timespec='microseconds').replace('+00:00','Z'),row[0],row[1]) for row in rows])
        db.execute('UPDATE monitoring_schema SET version=4 WHERE version<4')
    db.execute('''CREATE TABLE IF NOT EXISTS monitoring_webhooks(
        project_id TEXT PRIMARY KEY,url TEXT NOT NULL,secret TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS monitoring_notifications(
        id TEXT PRIMARY KEY,project_id TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL,
        state TEXT NOT NULL,attempts INTEGER NOT NULL,next_at TEXT,error TEXT,lease_until TEXT,acknowledged_at TEXT)''')
    db.execute('''CREATE INDEX IF NOT EXISTS monitoring_notifications_scope_time
        ON monitoring_notifications(project_id,created_at DESC,id DESC)''')
    db.execute('''CREATE INDEX IF NOT EXISTS monitoring_notifications_pending
        ON monitoring_notifications(state,next_at,lease_until,created_at,id)''')
    db.execute('''CREATE TABLE IF NOT EXISTS monitoring_sessions(
        project_id TEXT NOT NULL,session_id TEXT NOT NULL,PRIMARY KEY(project_id,session_id))''')
    db.execute('''INSERT OR IGNORE INTO monitoring_sessions SELECT r.id,j.value
        FROM monitoring_records r,json_each(r.payload,'$.session_ids') j WHERE r.kind='project' ''')
    db.execute("""CREATE INDEX IF NOT EXISTS monitoring_event_sources ON monitoring_records(
        kind,project_id,json_extract(payload,'$.event_id'),json_extract(payload,'$.sweep_id'))""")
    db.execute("""CREATE INDEX IF NOT EXISTS monitoring_search_marks ON monitoring_records(
        kind,project_id,json_extract(payload,'$.search_session_id'),created_at DESC,id DESC)""")
    db.execute("CREATE INDEX IF NOT EXISTS monitoring_state ON monitoring_records(kind,project_id,json_extract(payload,'$.state'),id)")
    db.execute("""CREATE INDEX IF NOT EXISTS monitoring_open_run ON monitoring_records(kind,project_id,
        json_extract(payload,'$.state'),json_extract(payload,'$.run_session_id'),json_extract(payload,'$.baseline_id'),id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS monitoring_receiver_pending ON monitoring_records(
        kind,project_id,json_extract(payload,'$.analysis.state'),
        json_extract(payload,'$.source_refs[#-1].session_id'),json_extract(payload,'$.last_seen') DESC,id DESC)""")


def install_sweep_index(db):
    """Portable UTC index; no custom-function index on original measurements."""
    import json
    from ..contracts import parse_time
    def canonical(value):
        return parse_time(value).isoformat(timespec='microseconds').replace('+00:00','Z')
    # Upgrade an unpublished development index without touching source rows.
    db.execute('DROP INDEX IF EXISTS monitoring_sweeps_session_time')
    db.execute('CREATE INDEX IF NOT EXISTS monitoring_investigations_state_time ON investigations(state,created_at,id)')
    db.execute('''CREATE TABLE IF NOT EXISTS monitoring_sweep_index(
        sweep_id TEXT PRIMARY KEY REFERENCES sweeps(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT NOT NULL,
        min_hz INTEGER NOT NULL,max_hz INTEGER NOT NULL)''')
    db.execute('''CREATE INDEX IF NOT EXISTS monitoring_sweep_index_scope_time
        ON monitoring_sweep_index(session_id,started_at DESC,sweep_id DESC)''')
    cursor=db.execute('''SELECT s.id,s.session_id,s.started_at,s.ended_at,s.frequencies_json
        FROM sweeps s LEFT JOIN monitoring_sweep_index i ON i.sweep_id=s.id WHERE i.sweep_id IS NULL''')
    while True:
        rows=cursor.fetchmany(128)
        if not rows:break
        values=[]
        for row in rows:
            frequencies=json.loads(row[4])
            values.append((row[0],row[1],canonical(row[2]),canonical(row[3]),min(frequencies),max(frequencies)))
        db.executemany('INSERT INTO monitoring_sweep_index VALUES (?,?,?,?,?,?)',values)
