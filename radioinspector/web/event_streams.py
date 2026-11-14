"""A listener owns its HTTP subscriptions, never the shared monitoring service."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import select
import socket
import threading
import time


DRAIN_TIMEOUT_SECONDS = 5.0
_WRITE_POLL_SECONDS = .05
_WRITE_CHUNK_BYTES = 64 * 1024
_NO_WRITE_OVERRIDE = object()


def peer_disconnected(error: OSError) -> bool:
    return isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)) or error.errno in {
        errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, errno.ENOTCONN,
        errno.EBADF, getattr(errno, 'ESHUTDOWN', -1), 10038, 10053, 10054, 10058,
    }


class _ActiveStream:
    def __init__(self, connection):
        self.connection = connection
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._wakeup = None
        self._writing = False
        self.restore_failed = False

    def bind_wakeup(self, notifier):
        with self._lock:
            self._wakeup = notifier
            if self._stopped.is_set():
                notifier.set()

    @property
    def stopped(self):
        return self._stopped.is_set()

    @contextmanager
    def writer(self, output):
        previous_timeout = self.connection.gettimeout()
        previous_write = vars(output).get('write', _NO_WRITE_OVERRIDE)
        mode_changed = installed = False
        try:
            self.connection.setblocking(False)
            mode_changed = True
            output.write = self.write
            installed = self._writing = True
            yield
        finally:
            self._writing = False
            try:
                try:
                    if installed:
                        if previous_write is _NO_WRITE_OVERRIDE:
                            del output.write
                        else:
                            output.write = previous_write
                finally:
                    if mode_changed:
                        self.connection.settimeout(previous_timeout)
            except BaseException:
                self.restore_failed = True
                raise

    def write(self, data):
        if not self._writing:
            raise RuntimeError('HTTP event stream writer is not active')
        view = memoryview(data).cast('B')
        offset = 0
        while offset < len(view):
            if self.stopped:
                raise ConnectionAbortedError(errno.ECONNABORTED, 'HTTP event stream stopped')
            try:
                sent = self.connection.send(view[offset:offset + _WRITE_CHUNK_BYTES])
            except BlockingIOError:
                # Some Winsock sends remain blocked after shutdown(SD_BOTH).
                # Nonblocking send preserves the known partial offset, while
                # this bounded poll always returns to the original stop flag.
                select.select([], [self.connection], [], _WRITE_POLL_SECONDS)
                continue
            if sent == 0:
                raise ConnectionResetError(errno.ECONNRESET, 'HTTP event stream peer closed')
            offset += sent
        return offset

    def peer_closed(self):
        if self.stopped:
            return True
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            return False
        except (OSError, ValueError):
            # Only this handler reads the socket. An invalid descriptor or EOF
            # means its own subscription ended, not a device/guard command.
            return True

    def stop(self):
        with self._lock:
            self._stopped.set()
            if self._wakeup is not None:
                self._wakeup.set()
        try:
            # Signal the owned socket too, but the cooperative writer does not
            # rely on shutdown interrupting a native blocking send. The handler
            # remains responsible for final socket close; no fd is reused here.
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            # It may already be disconnected. Draining below, not this syscall,
            # proves that the reader/encoder has relinquished archive access.
            pass


class EventStreams:
    def __init__(self):
        self._condition = threading.Condition()
        self._active = set()
        self._closing = False
        self._restore_failed = False

    @contextmanager
    def register(self, connection, *, writer=None):
        with self._condition:
            stream = None if self._closing else _ActiveStream(connection)
            if stream is not None:
                self._active.add(stream)
        try:
            if stream is None or writer is None:
                yield stream
            else:
                with stream.writer(writer):
                    yield stream
        finally:
            if stream is not None:
                with self._condition:
                    self._restore_failed |= stream.restore_failed
                    self._active.remove(stream)
                    self._condition.notify_all()

    @property
    def active_count(self):
        with self._condition:
            return len(self._active)

    def close(self):
        """Stop admission and await all previously admitted read/encode work.

        A timeout is a failure, retaining archive/writer ownership. The same
        object may be retried after the blocked work actually exits.
        """
        deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        with self._condition:
            self._closing = True
            active = tuple(self._active)
        for stream in active:
            stream.stop()
        with self._condition:
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('HTTP event streams still own archive work; shutdown not confirmed')
                self._condition.wait(remaining)
            if self._restore_failed:
                raise RuntimeError('HTTP event stream socket restoration failed; shutdown not confirmed')
