"""One HTTP write per offered input, even when a local TCP handshake stalls."""
from __future__ import annotations

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
        raise TimeoutError('Producer request deadline elapsed')
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
        sock = RequestSocket(family, deadline)
        outcome = 'failed'
        try:
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
        finally:
            if outcome != 'connected':
                sock.close()
            attempts.append(dict(outcome=outcome, timeout_ms=budget * 1000,
                                 elapsed_ms=(time.monotonic() - started) * 1000))


class ProducerHTTPHandler(HTTPHandler):
    def __init__(self, attempts):
        super().__init__()
        self.attempts = attempts

    def http_open(self, request):
        if request.get_method() != 'POST' or request.has_proxy():
            return super().http_open(request)
        try:
            address = ipaddress.ip_address(urlsplit(request.full_url).hostname)
        except ValueError:
            return super().http_open(request)
        if not address.is_loopback:
            return super().http_open(request)
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        deadline = time.monotonic() + request.timeout

        def connection(authority, *, timeout):
            http = HTTPConnection(authority, timeout=timeout)
            # Keep the standard HTTP/1 request, response and close behavior.
            # Only its TCP socket factory can replace an unconnected socket.
            http._create_connection = partial(connect_loopback, family=family,
                                              deadline=deadline, attempts=self.attempts)
            return http

        return self.do_open(connection, request)
