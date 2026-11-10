"""Both archive importers respect the exact UTF-8 wire budget."""
import json
import tempfile
import unittest
from pathlib import Path

from radioinspector.archive_ingest import (
    ArchiveIngestError, BATCH_ENVELOPE_BYTES, _encode_request,
    _server_batch_max_request_bytes, import_archive, parse_archive_document,
)
from radioinspector.archive_stream import STREAM_FORMAT, import_stream
from tests.test_archive_ingest import FakeClient, make_document, make_session


class ByteLimitClient(FakeClient):
    base_url = 'http://fake'


class ArchiveByteLimitTests(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.sid = self.session['id']
        self.payload = make_document(self.sid, *(s['id'] for s in self.session['streams']),
                                     sweep_count=5, observation_count=0)
        for item in self.payload['sweeps']:
            item['settings'] = {'note': 'я' * 20}
        self.document = parse_archive_document(self.payload, self.sid)

    def client(self, maximum):
        client = ByteLimitClient(self.session, limit=3)
        capabilities = client.capabilities()
        capabilities['features']['measurement_batch_max_request_bytes'] = maximum
        client.capabilities = lambda: capabilities
        return client

    def run_import(self, client, streaming, **options):
        if not streaming:
            return import_archive(client, self.document, self.sid, **options)
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source.ndjson'
            lines = [{'format': STREAM_FORMAT, 'format_version': 1, 'session_id': self.sid}]
            lines += [{'kind': 'sweep', 'measurement': item} for item in self.payload['sweeps']]
            source.write_text('\n'.join(json.dumps(x) for x in lines) + '\n', encoding='utf-8')
            return import_stream(client, source, self.sid, **options)

    def test_exact_two_item_budget_splits_without_losing_ids(self):
        from radioinspector.contracts import to_primitive
        maximum = len(_encode_request({'items': [to_primitive(x) for x in self.document.sweeps[:2]]}))
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                client = self.client(maximum)
                result = self.run_import(client, streaming)
                posts = [x for x in client.calls if x[0] == 'post']
                self.assertEqual(result['batches'], 3)
                self.assertEqual([len(x[-1]) for x in posts], [2, 2, 1])
                self.assertEqual([i['id'] for x in posts for i in x[-1]],
                                 [i.id for i in self.document.sweeps])
                self.assertTrue(all(len(_encode_request({'items': x[-1]})) <= maximum for x in posts))

    def test_oversized_last_record_is_rejected_before_any_post_including_dry_run(self):
        self.payload['sweeps'][-1]['settings'] = {'note': 'я' * 1000}
        self.document = parse_archive_document(self.payload, self.sid)
        for streaming in (False, True):
            for dry_run in (False, True):
                client = self.client(2000)
                with self.subTest(streaming=streaming, dry_run=dry_run), self.assertRaises(ArchiveIngestError) as caught:
                    self.run_import(client, streaming, dry_run=dry_run)
                self.assertEqual(caught.exception.code, 'measurement_too_large')
                self.assertFalse(any(x[0] == 'post' for x in client.calls))

    def test_old_server_fallback_and_invalid_capabilities(self):
        self.assertEqual(_server_batch_max_request_bytes({'features': {}}), 16 * 1024 * 1024)
        for value in (None, True, '1000', 0, BATCH_ENVELOPE_BYTES - 1):
            with self.subTest(value=value), self.assertRaises(ArchiveIngestError):
                _server_batch_max_request_bytes({'features': {'measurement_batch_max_request_bytes': value}})
        for streaming in (False, True):
            client = ByteLimitClient(self.session, limit=3)
            self.assertEqual(self.run_import(client, streaming)['batches'], 2)
