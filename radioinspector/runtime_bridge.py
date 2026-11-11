"""Private owned-supervisor IPC and cooperative archive-writer admission.

This module opens no listener or database, touches no hardware, and never
signals a PID. Enter the bridge FIRST in an ExitStack so its writer lease is
released LAST, after successful archive/resource closure.
"""
from __future__ import annotations

from contextlib import contextmanager
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Mapping, Optional


STATUS_PATH = '/internal/runtime/v1/status'
SHUTDOWN_PATH = '/internal/runtime/v1/shutdown'
MAX_SHUTDOWN_BYTES = 4096
# A failed cleanup must not release the lease merely because its ExitStack is GC'd.
_RETAINED = {}
_RETAINED_LOCK = threading.RLock()


class RuntimeBridgeError(RuntimeError):
    """Safe error envelope; values never include credentials or request bodies."""
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


def _safe_path(path: Path) -> None:
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeBridgeError(500, 'runtime_unsafe_path')
    for ancestor in reversed((path,) + tuple(path.parents)):
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeBridgeError(500, 'runtime_unsafe_path')
        if os.name != 'nt' and stat.S_ISDIR(metadata.st_mode):
            if metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX:
                raise RuntimeBridgeError(500, 'runtime_unsafe_path')


def _private(metadata, *, directory=False) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode):
        raise RuntimeBridgeError(500, 'runtime_unsafe_state')
    if os.name != 'nt':
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise RuntimeBridgeError(500, 'runtime_unsafe_permissions')
        if not directory and metadata.st_nlink != 1:
            raise RuntimeBridgeError(500, 'runtime_unsafe_state')


def _writer_lease(data_directory: Path):
    _safe_path(data_directory)
    metadata = data_directory.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeBridgeError(500, 'runtime_invalid_data_directory')
    if os.name != 'nt' and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022):
        raise RuntimeBridgeError(500, 'runtime_unsafe_permissions')
    state = data_directory / '.ori-runtime'
    state.mkdir(mode=0o700, exist_ok=True)
    _safe_path(state)
    _private(state.lstat(), directory=True)
    path = state / 'writer.lock'
    _safe_path(path)
    try:
        _private(path.lstat())
    except FileNotFoundError:
        pass
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0)
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    lease = os.fdopen(descriptor, 'r+b', buffering=0)
    try:
        os.set_inheritable(descriptor, False)
        _private(os.fstat(descriptor))
        if os.name == 'nt':
            import msvcrt
            lease.seek(0)
            # One byte overlaps fs2's entire-file LockFileEx range, including past EOF.
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        lease.close()
        raise RuntimeBridgeError(409, 'runtime_writer_busy') from error
    except BaseException:
        lease.close()
        raise
    return lease


def _shutdown_payload(value):
    if isinstance(value, bytes):
        if len(value) > MAX_SHUTDOWN_BYTES:
            raise RuntimeBridgeError(413, 'runtime_request_too_large')
        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError('duplicate field')
                result[key] = item
            return result
        def invalid_constant(_value):
            raise ValueError('nonfinite value')
        try:
            value = json.loads(value.decode('utf-8', errors='strict'),
                               object_pairs_hook=pairs, parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise RuntimeBridgeError(400, 'runtime_invalid_shutdown') from None
    fields = {'version', 'preserve_resume_requested', 'reason', 'acknowledge_coverage_gap'}
    if (type(value) is not dict or set(value) - fields
            or type(value.get('version')) is not int or value['version'] != 1
            or value.get('preserve_resume_requested') is not True
            or type(value.get('reason')) is not str
            or value['reason'] not in ('shutdown', 'cutover', 'rollback')
            or type(value.get('acknowledge_coverage_gap', False)) is not bool):
        raise RuntimeBridgeError(400, 'runtime_invalid_shutdown')
    return dict(version=1, preserve_resume_requested=True, reason=value['reason'],
                acknowledge_coverage_gap=value.get('acknowledge_coverage_gap', False))


class RuntimeBridge:
    """One supervised core lifecycle. The parent supplies HTTP and cleanup ownership."""
    @classmethod
    def from_environment(cls, session_database, *, environ: Optional[Mapping[str, str]] = None):
        environment = os.environ if environ is None else environ
        if not any(key.startswith('ORI_RUNTIME_') for key in environment):
            return None
        token = environment.get('ORI_RUNTIME_CONTROL_TOKEN')
        instance = environment.get('ORI_RUNTIME_INSTANCE_ID')
        if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token)
                or not isinstance(instance, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', instance)):
            raise RuntimeBridgeError(500, 'runtime_invalid_configuration')
        configured = environment.get('ORI_RUNTIME_DATA_DIR')
        database = None
        if session_database and str(session_database) != ':memory:':
            database = Path(session_database).absolute()
            _safe_path(database)
        if configured:
            directory = Path(configured)
            if not directory.is_absolute():
                raise RuntimeBridgeError(500, 'runtime_invalid_configuration')
        elif database is not None:
            directory = database.parent
        else:
            raise RuntimeBridgeError(500, 'runtime_invalid_configuration')
        if database is not None:
            if database.parent != directory:
                raise RuntimeBridgeError(500, 'runtime_database_outside_data_directory')
            if database.exists() and database.stat().st_nlink != 1:
                raise RuntimeBridgeError(500, 'runtime_unsafe_path')
        try:
            lease = _writer_lease(directory)
        except OSError as error:
            raise RuntimeBridgeError(500, 'runtime_writer_unavailable') from error
        return cls(token, instance, directory, lease)

    def __init__(self, token, instance, directory, lease):
        self._token, self._instance = token, instance
        self.data_directory = directory
        self._lease = lease
        self._condition = threading.Condition(threading.RLock())
        self._active = 0
        self._quiescing = False
        self._pending = None
        self._shutdown_thread = None
        self._shutdown_failure = None
        self._cleanup_failed = False
        self._closed = False
        self._httpd = self._monitoring = None

    def __enter__(self):
        return self

    def _retain(self):
        self._cleanup_failed = True
        with _RETAINED_LOCK:
            _RETAINED[id(self)] = self

    def __exit__(self, error_type, _error, _traceback):
        with self._condition:
            if error_type is not None:
                self._retain()
                return False
        self.close()
        return False

    def close(self):
        """ExitStack-only close; a previous cleanup failure needs an explicit safe retry."""
        with self._condition:
            if self._cleanup_failed:
                raise RuntimeBridgeError(500, 'runtime_archive_cleanup_not_confirmed')
        self.release_after_archive_closed()

    def release_after_archive_closed(self):
        """Caller confirms archive, instruments and all writer resources actually closed."""
        with self._condition:
            if self._active:
                self._retain()
                raise RuntimeBridgeError(500, 'runtime_mutations_not_drained')
            self._closed = True
            self._quiescing = True
            self._cleanup_failed = False
            self._condition.notify_all()
            if self._lease is not None:
                self._lease.close()
                self._lease = None
            with _RETAINED_LOCK:
                _RETAINED.pop(id(self), None)

    def attach(self, httpd, monitoring):
        with self._condition:
            if self._closed or self._httpd is not None:
                raise RuntimeBridgeError(500, 'runtime_already_attached_or_closed')
            self._httpd, self._monitoring = httpd, monitoring

    def authorize_control(self, peer, authorization):
        try:
            address = ipaddress.ip_address(peer[0])
            mapped = getattr(address, 'ipv4_mapped', None)
            loopback = address.is_loopback or (mapped is not None and mapped.is_loopback)
        except (ValueError, TypeError, IndexError):
            loopback = False
        if not loopback:
            raise RuntimeBridgeError(403, 'runtime_loopback_required')
        if (not isinstance(authorization, (list, tuple)) or len(authorization) != 1
                or not isinstance(authorization[0], str) or len(authorization[0]) > 512):
            raise RuntimeBridgeError(401, 'runtime_unauthorized')
        value = authorization[0]
        if not value.startswith('Bearer '):
            raise RuntimeBridgeError(401, 'runtime_unauthorized')
        candidate = value[7:]
        if not candidate.isascii() or not hmac.compare_digest(candidate, self._token):
            raise RuntimeBridgeError(401, 'runtime_unauthorized')

    def _monitoring_state(self):
        monitoring = self._monitoring
        if monitoring is None:
            return False, False
        # The supervisor's loopback deadline is 500 ms. Long arm/device work must
        # cause a prompt retryable refusal, not an unflushed quiescing response.
        if not monitoring.lock.acquire(timeout=.1):
            raise RuntimeBridgeError(503, 'runtime_state_busy')
        try:
            event = getattr(monitoring, 'startup_recovery_complete', None)
            complete = bool(event is not None and event.is_set()
                            and getattr(monitoring, 'startup_recovery_error', None) is None
                            and not monitoring._closed)
            return complete, bool(monitoring.runtime)
        finally:
            monitoring.lock.release()

    def status(self):
        with self._condition:
            complete, armed = self._monitoring_state()
            owned = bool(self._lease is not None and not self._lease.closed)
            return dict(version=1, instance_id=self._instance, pid=os.getpid(),
                        ready=bool(owned and complete and self._httpd is not None
                                   and not self._quiescing and not self._cleanup_failed and not self._closed),
                        preflight_complete=bool(complete and not self._cleanup_failed),
                        guard_armed=armed, writer_owned=owned)

    @contextmanager
    def mutation(self):
        """Enter AFTER bounded body parsing, immediately around actual mutation dispatch."""
        with self._condition:
            if self._quiescing or self._closed:
                raise RuntimeBridgeError(503, 'runtime_quiescing')
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def control_response(self, method, target, *, peer, authorization, payload=None):
        if not isinstance(target, str) or not target.startswith('/internal/runtime/'):
            return None
        self.authorize_control(peer, authorization)
        if target == STATUS_PATH and method == 'GET':
            return 200, self.status()
        if target != SHUTDOWN_PATH or method != 'POST':
            raise RuntimeBridgeError(404, 'runtime_route_not_found')
        request = _shutdown_payload(payload)
        with self._condition:
            if self._closed or self._httpd is None or self._monitoring is None:
                raise RuntimeBridgeError(503, 'runtime_not_attached')
            if self._pending is not None:
                if self._pending == request:
                    return 202, {'version': 1, 'accepted': True}
                raise RuntimeBridgeError(409, 'runtime_shutdown_already_pending')
            _complete, armed = self._monitoring_state()
            if request['reason'] == 'cutover' and armed and not request['acknowledge_coverage_gap']:
                raise RuntimeBridgeError(409, 'runtime_guard_armed')
            self._quiescing = True
            self._pending = request
            self._shutdown_failure = None
            return 202, {'version': 1, 'accepted': True}

    def response_not_flushed(self):
        """Cancel only an unflushed pending response; never cancel a started shutdown."""
        with self._condition:
            if self._pending is not None and self._shutdown_thread is None and not self._closed:
                self._pending = None
                self._quiescing = False
                self._condition.notify_all()

    def after_response_flushed(self):
        """Start owned shutdown work only after the handler flushes its HTTP 202."""
        with self._condition:
            if self._pending is None or self._shutdown_thread is not None or self._closed:
                return
            self._shutdown_thread = threading.Thread(target=self._drain_and_shutdown,
                name='owned-runtime-shutdown', daemon=True)
            self._shutdown_thread.start()

    def _drain_and_shutdown(self):
        with self._condition:
            while self._active and not self._closed:
                self._condition.wait()
            if self._closed:
                return
            request = self._pending
            while not self._closed:
                try:
                    _complete, armed = self._monitoring_state()
                    break
                except RuntimeBridgeError:
                    self._condition.wait(.05)
            if self._closed:
                return
            if request['reason'] == 'cutover' and armed and not request['acknowledge_coverage_gap']:
                # An earlier admitted arm completed while draining. Keep this owner alive.
                self._pending = None
                self._quiescing = False
                self._shutdown_thread = None
                self._shutdown_failure = 'runtime_guard_armed_after_drain'
                self._condition.notify_all()
                return
            httpd = self._httpd
        try:
            # Packaged launcher waits on a separate event; direct web.serve waits on HTTP.
            stop = getattr(httpd, 'runtime_stop', None)
            if stop is not None:
                stop()
            httpd.shutdown()
        except Exception:
            # Keep writer ownership and admission closed if closure cannot be initiated.
            with self._condition:
                self._shutdown_failure = 'runtime_shutdown_failed'
