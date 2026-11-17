import io
import json
from http.client import IncompleteRead
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

from benchmarks.ori_bench.client import Client, HTTPFailure


class HttpFailureDiagnostics(unittest.TestCase):
    def fail_request(self, body, *, read_error=None):
        failure = HTTPError('http://127.0.0.1:1/api/v1/test', 503, 'Unavailable', {}, io.BytesIO(body))
        failure.read = Mock(wraps=failure.read) if read_error is None else Mock(side_effect=read_error)
        failure.close = Mock(wraps=failure.close)
        client = Client(SimpleNamespace(base_url='http://127.0.0.1:1', request_timeout=1))
        client.opener = Mock()
        client.opener.open.side_effect = failure
        with self.assertRaises(HTTPFailure) as caught:
            client.open('/api/v1/test')
        self.assertEqual(caught.exception.status, 503)
        failure.read.assert_called_once_with(16385)
        failure.close.assert_called_once_with()
        client.opener.open.assert_called_once()
        return caught.exception

    def test_archive_budget_code_survives_without_server_message_or_payload(self):
        body = json.dumps(dict(contract='openradioinspector-platform', contract_version=1,
            error=dict(code='archive_query_budget', message='private operator secret'))).encode()
        error = self.fail_request(body)
        self.assertEqual(error.server_code, 'archive_query_budget')
        self.assertIn('archive_query_budget', str(error))
        self.assertNotIn('private operator secret', str(error))

    def test_oversized_error_envelope_does_not_enter_diagnostics(self):
        body = json.dumps(dict(contract='openradioinspector-platform', contract_version=1,
            error=dict(code='archive_query_budget', message='x' * 20000))).encode()
        error = self.fail_request(body)
        self.assertIsNone(error.server_code)
        self.assertEqual(str(error), 'HTTP request failed with status 503')

    def test_untrusted_unknown_and_malformed_error_fields_are_omitted(self):
        for body in (b'not JSON', b'\xff', b'[]', b'{"error":{"code":"private_password"}}',
                     b'{"contract":"openradioinspector-platform","contract_version":1,"error":{"code":{"private":"secret"}}}'):
            with self.subTest(body=body):
                error = self.fail_request(body)
                self.assertIsNone(error.server_code)
                self.assertEqual(str(error), 'HTTP request failed with status 503')

    def test_error_body_read_failure_retains_original_http_status(self):
        for failure in (OSError('private transport detail'), IncompleteRead(b'private body', 100)):
            with self.subTest(kind=type(failure).__name__):
                error = self.fail_request(b'', read_error=failure)
                self.assertIsNone(error.server_code)
                self.assertEqual(str(error), 'HTTP request failed with status 503')
