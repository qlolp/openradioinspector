"""Keep owned loopback HTTP tests independent of the host's reverse DNS.

HTTPServer.server_bind calls socket.getfqdn even for literal loopback addresses.
On hosted macOS 15/26 with Framework Python that lookup can block for >30s:
https://github.com/actions/setup-python/issues/1223
Only tests use this scoped helper; production DNS and bind behavior are unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
import socket
from unittest.mock import patch


@contextmanager
def local_loopback_names():
    original = socket.getfqdn

    def getfqdn(name=""):
        if name in ("127.0.0.1", "::1"):
            return "localhost"
        return original(name)

    with patch.object(socket, "getfqdn", getfqdn):
        yield
