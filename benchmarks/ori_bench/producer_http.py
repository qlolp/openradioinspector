"""Bound a direct loopback TCP handshake before the original HTTP write."""
from __future__ import annotations

import errno
from functools import partial
from http.client import HTTPConnection
import ipaddress
import socket
import time
from urllib.parse import urlsplit
from urllib.request import HTTPHandler


# The final dial receives the balance of the same request deadline. A healthy
# loopback request uses exactly one socket.
_CONNECT_LIMITS_SECONDS = (.25, .5, 1., None)


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError('HTTP request deadline elapsed')
    return seconds


class RequestSocket(socket.socket):
    def __init__(self, family, deadline):
        super().__init__(family, socket.SOCK_STREAM)
        self.deadline = deadline

    def sendall(self, data, flags=0):
        self.settimeout(remaining(self.deadline))
        return super().sendall(data, flags)

    def recv_into(self, buffer, nbytes=0, flags=0):
        # HTTPResponse reads headers and bodies via socket.makefile/SocketIO.
        self.settimeout(remaining(self.deadline))
        return super().recv_into(buffer, nbytes, flags)


def connect_loopback(endpoint, timeout, source_address, *, family, deadline, attempts):
    """HTTPConnection invokes this dialer before it can send headers or a body."""
    for window in _CONNECT_LIMITS_SECONDS:
        started = time.monotonic()
        budget = min(window if window is not None else timeout, remaining(deadline))
        sock = None
        outcome = 'failed'
        error_codes = {}
        try:
            sock = RequestSocket(family, deadline)
            if source_address is not None:
                sock.bind(source_address)
            sock.settimeout(budget)
            sock.connect(endpoint)
            outcome = 'connected'
            return sock
        except TimeoutError:
            outcome = 'timeout'
            if window is None or time.monotonic() >= deadline:
                raise
        except OSError as error:
            if error.errno not in (errno.ENOBUFS, 10055) and getattr(error, 'winerror', None) != 10055:
                raise
            outcome = 'no_buffers'
            error_codes['errno'] = error.errno
            if getattr(error, 'winerror', None) is not None:
                error_codes['winerror'] = error.winerror
            if window is None or started + window >= deadline:
                raise
        finally:
            if sock is not None and outcome != 'connected':
                sock.close()
            attempts.append(dict(outcome=outcome, timeout_ms=budget * 1000,
                                 elapsed_ms=(time.monotonic() - started) * 1000, **error_codes))
        if outcome == 'no_buffers':
            # Give the host the remainder of this dial window to free socket resources.
            time.sleep(max(0., started + budget - time.monotonic()))


class LoopbackHTTPHandler(HTTPHandler):
    def __init__(self, method, record):
        super().__init__()
        self.method = method
        self.record = record

    def http_open(self, request):
        if request.get_method() != self.method or request.has_proxy():
            return super().http_open(request)
        try:
            address = ipaddress.ip_address(urlsplit(request.full_url).hostname)
        except ValueError:
            return super().http_open(request)
        if not address.is_loopback:
            return super().http_open(request)
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        deadline = time.monotonic() + request.timeout
        # Concurrent export and cursor/QoS requests must keep disjoint traces.
        attempts = []

        def connection(authority, *, timeout):
            http = HTTPConnection(authority, timeout=timeout)
            # Keep the standard HTTP/1 request, response and close behavior.
            # Only its TCP socket factory can replace an unconnected socket.
            http._create_connection = partial(connect_loopback, family=family,
                                              deadline=deadline, attempts=attempts)
            return http

        try:
            return self.do_open(connection, request)
        finally:
            self.record(request, attempts)
