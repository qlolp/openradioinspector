"""Cooperative writer admission for an unsupervised persistent Python server.

This module opens no archive, listener or instrument. Enter this owner FIRST in
an ExitStack, before SQLite, so it exits LAST after all writers close. A failed
cleanup retains a strong lease until the caller explicitly proves closure.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import threading

from .runtime_bridge import RuntimeBridgeError, _safe_path, _writer_lease


_RETAINED: dict[int, ArchiveWriterOwner] = {}
_RETAINED_LOCK = threading.RLock()


def _checked_path(path: Path) -> None:
    _safe_path(path)
    # An untrusted owner can rename a child even when others cannot write its
    # directory. Trust only this user and system-owned ancestors on POSIX.
    # Windows junctions can alias paths without appearing as POSIX symlinks.
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if os.name == 'nt':
            if getattr(metadata, 'st_file_attributes', 0) & 0x400:
                raise RuntimeBridgeError(500, 'runtime_unsafe_path')
        elif stat.S_ISDIR(metadata.st_mode) and metadata.st_uid not in (0, os.geteuid()):
            raise RuntimeBridgeError(500, 'runtime_unsafe_permissions')


def _directory(path: Path, *, creation_anchor: bool = False) -> None:
    _checked_path(path)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeBridgeError(500, 'runtime_invalid_data_directory')
    if os.name != 'nt':
        # A sticky system temp directory may contain newly created private
        # children. The actual data directory itself must always be owned and
        # never writable by another user (the same rule as the Rust writer).
        if creation_anchor and metadata.st_mode & stat.S_ISVTX and metadata.st_uid in (0, os.geteuid()):
            return
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise RuntimeBridgeError(500, 'runtime_unsafe_permissions')


def _prepare_directory(directory: Path) -> None:
    _checked_path(directory)
    missing = []
    cursor = directory
    while True:
        try:
            cursor.lstat()
            break
        except FileNotFoundError:
            missing.append(cursor)
            cursor = cursor.parent
    _directory(cursor, creation_anchor=bool(missing))
    for path in reversed(missing):
        _directory(path.parent, creation_anchor=True)
        try:
            # parents=True would create intermediate directories with the process
            # defaults. Create/check every component explicitly instead.
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _directory(path)
    _directory(directory)


def _archive_file(path: Path) -> None:
    _checked_path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeBridgeError(500, 'runtime_unsafe_path')
    if os.name != 'nt' and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022):
        raise RuntimeBridgeError(500, 'runtime_unsafe_permissions')


def archive_writer_owner(session_database: str | os.PathLike[str] | None):
    """Acquire the shared writer lease without opening or creating a database.

    None/:memory: are inert. Use the returned database_path for SQLite too, so
    tilde/relative input cannot acquire a lease for a different literal filename.
    Existing owned 0755 data parents remain unchanged; new parents request 0700.
    """
    if session_database is None:
        return None
    try:
        value = os.fspath(session_database)
        if value == ':memory:':
            return None
        if not isinstance(value, str) or not value.strip():
            raise RuntimeBridgeError(500, 'runtime_unsafe_path')
        database = Path(value).expanduser().absolute()
        _archive_file(database)
        _prepare_directory(database.parent)
        # The shared helper checks private state/lock permissions and uses flock
        # on Unix or the byte range overlapping fs2 LockFileEx on Windows.
        _archive_file(database.parent / '.ori-runtime' / 'writer.lock')
        lease = _writer_lease(database.parent)
    except RuntimeBridgeError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise RuntimeBridgeError(500, 'runtime_writer_unavailable') from error
    return ArchiveWriterOwner(database, lease)


class ArchiveWriterOwner:
    """One lease lifetime, independent of runtime credentials or HTTP state."""
    def __init__(self, database_path: Path, lease):
        self.database_path = database_path
        self.data_directory = database_path.parent
        self._lease = lease
        self._lock = threading.RLock()
        self._cleanup_failed = False

    def __enter__(self):
        with self._lock:
            if self._lease is None or self._lease.closed:
                raise RuntimeBridgeError(500, 'runtime_archive_owner_closed')
        return self

    def _retain(self):
        self._cleanup_failed = True
        with _RETAINED_LOCK:
            _RETAINED[id(self)] = self

    def __exit__(self, error_type, _error, _traceback):
        with self._lock:
            if error_type is not None:
                if self._lease is not None:
                    self._retain()
                return False
            self.close()
        return False

    def close(self):
        """Successful ordered cleanup only; a failed cleanup needs explicit proof."""
        with self._lock:
            if self._cleanup_failed:
                raise RuntimeBridgeError(500, 'runtime_archive_cleanup_not_confirmed')
            self.release_after_archive_closed()

    def release_after_archive_closed(self):
        """Caller has verified that the archive and every writer really closed."""
        with self._lock:
            if self._lease is not None:
                try:
                    self._lease.close()
                except BaseException:
                    self._retain()
                    raise
                self._lease = None
            self._cleanup_failed = False
            with _RETAINED_LOCK:
                _RETAINED.pop(id(self), None)
