"""Regression for macOS hosted-runner loopback reverse-DNS stalls."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import unittest
from unittest.mock import patch

from tests.loopback_dns import local_loopback_names


class LoopbackDNSTests(unittest.TestCase):
    def test_owned_http_bind_does_not_call_host_reverse_resolver(self):
        # Fail rather than sleep: this reproduces the exact HTTPServer startup
        # dependency without waiting for the hosted macOS resolver timeout.
        with patch.object(socket, "getfqdn", side_effect=AssertionError("host reverse DNS used")) as resolver:
            with local_loopback_names():
                server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
                try:
                    self.assertEqual(server.server_name, "localhost")
                    self.assertGreater(server.server_port, 0)
                finally:
                    server.server_close()
            resolver.assert_not_called()

    def test_only_explicit_loopback_names_bypass_resolver(self):
        with patch.object(socket, "getfqdn", return_value="original-result") as resolver:
            with local_loopback_names():
                for value in ("127.0.0.1", "::1"):
                    self.assertEqual(socket.getfqdn(value), "localhost")
                resolver.assert_not_called()
                for value in ("localhost", "", "0.0.0.0", "127.0.0.2", "::", "example.invalid"):
                    with self.subTest(name=value):
                        self.assertEqual(socket.getfqdn(value), "original-result")
                        resolver.assert_called_with(value)
                self.assertEqual(resolver.call_count, 6)

    def test_nonloopback_resolver_exceptions_are_not_hidden(self):
        with patch.object(socket, "getfqdn", side_effect=OSError("synthetic resolver failure")):
            with local_loopback_names(), self.assertRaisesRegex(OSError, "synthetic resolver failure"):
                socket.getfqdn("example.invalid")

    def test_scope_restores_resolver_after_exception_and_nested_use(self):
        with patch.object(socket, "getfqdn", return_value="original-result") as original:
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                with local_loopback_names():
                    outer = socket.getfqdn
                    with local_loopback_names():
                        self.assertEqual(socket.getfqdn("127.0.0.1"), "localhost")
                    self.assertIs(socket.getfqdn, outer)
                    raise RuntimeError("synthetic failure")
            self.assertIs(socket.getfqdn, original)


if __name__ == "__main__":
    unittest.main()
