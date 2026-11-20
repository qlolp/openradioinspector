"""Measure a recent archive window without deserializing spectrum arrays."""

from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from radioinspector.contracts import parse_time


@lru_cache(maxsize=1)
def _query():
    return files(__package__).joinpath('storage_window.sql').read_text(encoding='utf-8')


def sweep_storage_sample(store, session_id, stream_id):
    with store.read_snapshot() as db:
        count, arrays, columns, first, last = db.execute(_query(), (session_id, stream_id)).fetchone()
    try:
        wal_bytes = Path(str(Path(store.path).expanduser().resolve()) + '-wal').stat().st_size
    except FileNotFoundError:
        wal_bytes = 0
    except OSError:
        wal_bytes = None
    rate = None
    if count >= 2:
        seconds = (parse_time(last) - parse_time(first)).total_seconds()
        if seconds > 0:
            rate = 2 * (columns + 256 * count) / seconds
    return count, arrays / count if count else None, rate, wal_bytes
