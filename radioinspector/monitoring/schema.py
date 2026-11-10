"""Additive project schema, installed within SQLiteSessionStore migration."""

def install_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS monitoring_schema (version INTEGER NOT NULL)')
    version = db.execute('SELECT version FROM monitoring_schema').fetchone()
    if version and version[0] > 1:
        raise ValueError('Неподдерживаемая версия проектов наблюдения')
    db.execute('''CREATE TABLE IF NOT EXISTS monitoring_records (
        kind TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,id))''')
    db.execute('''CREATE INDEX IF NOT EXISTS monitoring_scope_time
        ON monitoring_records(kind,project_id,created_at,id)''')
    if not version:
        db.execute('INSERT INTO monitoring_schema VALUES (?)', (1,))
