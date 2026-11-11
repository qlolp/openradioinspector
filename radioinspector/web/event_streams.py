"""A listener owns its HTTP subscriptions, never the shared monitoring service."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import select
import socket
import threading
import time


DRAIN_TIMEOUT_SECONDS = 5.0


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

    def bind_wakeup(self, notifier):
        with self._lock:
            self._wakeup = notifier
            if self._stopped.is_set():
                notifier.set()

    @property
    def stopped(self):
        return self._stopped.is_set()

    def peer_closed(self):
        if self.stopped:
            return True
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
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
            # Wake a blocked send without closing/reusing another thread's fd.
            # The HTTP request handler remains responsible for final socket close.
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

    @contextmanager
    def register(self, connection):
        with self._condition:
            stream = None if self._closing else _ActiveStream(connection)
            if stream is not None:
                self._active.add(stream)
        try:
            yield stream
        finally:
            if stream is not None:
                with self._condition:
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
