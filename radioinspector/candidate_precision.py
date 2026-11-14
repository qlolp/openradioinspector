"""Versioned canonical candidate scalar bytes; historical REALs are not guessed."""
import math
import struct


SELECT = '''SELECT c.*,p.peak_level_bits_le,p.score_bits_le FROM candidates c
    LEFT JOIN ori_candidate_scalar_bits p ON p.candidate_id=c.id'''


def check_extension(db):
    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('ori_candidate_precision_schema','ori_candidate_scalar_bits')")}
    if tables and len(tables)!=2:
        raise ValueError('Candidate precision extension is incomplete')
    if tables:
        rows=db.execute('SELECT singleton,version FROM ori_candidate_precision_schema').fetchmany(2)
        if [tuple(row) for row in rows]!=[(1,1)]:
            raise ValueError('Candidate precision extension is unsupported')


def install_extension(db):
    db.execute('''CREATE TABLE IF NOT EXISTS ori_candidate_precision_schema(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL)''')
    db.execute('INSERT OR IGNORE INTO ori_candidate_precision_schema VALUES(1,1)')
    db.execute('''CREATE TABLE IF NOT EXISTS ori_candidate_scalar_bits(
        candidate_id TEXT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
        peak_level_bits_le BLOB CHECK(peak_level_bits_le IS NULL OR length(peak_level_bits_le)=8),
        score_bits_le BLOB CHECK(score_bits_le IS NULL OR length(score_bits_le)=8),
        CHECK(peak_level_bits_le IS NOT NULL OR score_bits_le IS NOT NULL))''')


def save(db,candidate):
    bits=[]
    for value in (candidate.peak_level,candidate.score):
        if value is not None and not math.isfinite(value):
            raise ValueError('Candidate scalar bytes must be finite')
        bits.append(struct.pack('<d',value) if value is not None else None)
    if bits==[None,None]:
        db.execute('DELETE FROM ori_candidate_scalar_bits WHERE candidate_id=?',(candidate.id,))
    else:
        db.execute('''INSERT INTO ori_candidate_scalar_bits VALUES(?,?,?)
            ON CONFLICT(candidate_id) DO UPDATE SET
            peak_level_bits_le=excluded.peak_level_bits_le,score_bits_le=excluded.score_bits_le''',
            (candidate.id,*bits))


def decode(stored,bits):
    if bits is None:
        return stored
    if not isinstance(bits,bytes) or len(bits)!=8 or stored is None:
        raise ValueError('Candidate scalar bytes are invalid')
    value=struct.unpack('<d',bits)[0]
    if not math.isfinite(value) or value!=stored:
        raise ValueError('Candidate scalar bytes disagree with stored REAL')
    return value


def validate_rows(db):
    check_extension(db)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ori_candidate_scalar_bits'").fetchone():
        return
    cursor=db.execute('''SELECT c.peak_level,c.score,p.peak_level_bits_le,p.score_bits_le
        FROM ori_candidate_scalar_bits p LEFT JOIN candidates c ON c.id=p.candidate_id''')
    while rows:=cursor.fetchmany(128):
        for peak,score,peak_bits,score_bits in rows:
            decode(peak,peak_bits)
            decode(score,score_bits)
