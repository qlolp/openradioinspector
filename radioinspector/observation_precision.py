"""Canonical float64 precision supplement, compatible with the v6 raw tables.

SQLite REAL may store -0.0 as integer zero. These bytes are original data, not a
regenerable display cache. Existing historical REAL values are never guessed or
rewritten; absence of a supplement means exactly the historical stored value.
"""
import math
import struct


def check_extension(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('ori_archive_schema','ori_observation_level_bits')")}
    if tables and len(tables) != 2:
        raise ValueError('Incomplete archive precision extension')
    if tables:
        rows = db.execute('SELECT singleton,version FROM ori_archive_schema').fetchmany(2)
        if [tuple(row) for row in rows] != [(1, 1)]:
            raise ValueError('Unsupported archive precision extension')


def install_extension(db):
    db.execute('''CREATE TABLE IF NOT EXISTS ori_archive_schema(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL)''')
    db.execute('INSERT OR IGNORE INTO ori_archive_schema VALUES(1,1)')
    db.execute('''CREATE TABLE IF NOT EXISTS ori_observation_level_bits(
        observation_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE,
        level_bits_le BLOB NOT NULL CHECK(length(level_bits_le)=8))''')


def decode_level(stored, bits):
    if bits is None:
        return stored
    if not isinstance(bits, bytes) or len(bits) != 8 or stored is None:
        raise ValueError('Invalid original observation level bytes')
    value = struct.unpack('<d', bits)[0]
    if not math.isfinite(value) or value != stored:
        raise ValueError('Observation level bytes disagree with stored REAL')
    return value
