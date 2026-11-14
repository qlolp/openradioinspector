"""Actual History wire shapes and exact shared scalar ranges, without a server."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jsonschema

ROOT = Path(__file__).resolve().parents[2]


class HistoryRuntimeContractTests(unittest.TestCase):
    def document(self):
        from tools.contracts.runtime_contract import compose_runtime_document, load_extension
        baseline = json.loads((ROOT / 'contracts/generated/openapi-v1.json').read_text())
        return compose_runtime_document(baseline, load_extension())

    def fixture(self):
        return json.loads((ROOT / 'contracts/fixtures/history-runtime-v1.json').read_text())

    def validator(self, name):
        document = self.document()
        return jsonschema.Draft202012Validator({
            '$ref': '#/components/schemas/' + name, 'components': document['components']})

    def test_actual_history_routes_have_independent_features_and_binary_parquet(self):
        document = self.document()
        expected = {
            ('/api/v1/history/jobs', 'get'), ('/api/v1/history/jobs', 'post'),
            ('/api/v1/history/jobs/{id}', 'get'),
            ('/api/v1/history/jobs/{id}/result', 'get'),
            ('/api/v1/history/jobs/{id}/cancel', 'post'),
            ('/api/v1/history/jobs/{id}/pages', 'post'),
            ('/api/v1/history/jobs/{id}/exports/{table}', 'get'),
        }
        for path, method in expected:
            operation = document['paths'][path][method]
            self.assertEqual(operation['x-ori-backends'], ['rust-candidate'])
            self.assertEqual(operation['x-ori-feature-requirements'], ['derived_history_jobs'])
            self.assertIn('503', operation['responses'])
            if method == 'post':
                self.assertEqual(operation['x-max-request-bytes'], 65536)
        viewport = document['paths']['/api/v1/history/waterfall']['get']
        self.assertEqual(viewport['x-ori-feature-requirements'], ['derived_history_viewport'])
        self.assertEqual({p['name'] for p in viewport['parameters']}, {
            'session_id', 'stream_id', 'min_frequency_hz', 'max_frequency_hz',
            'columns', 'rows', 'start_us', 'end_us', 'cursor'})
        export = document['paths']['/api/v1/history/jobs/{id}/exports/{table}']['get']
        self.assertEqual(set(export['responses']['200']['content']), {'application/vnd.apache.parquet'})

    def test_common_fixtures_validate_signed_extremes_nullable_unsigned_boundary_and_open_json(self):
        names = {
            'request': 'HistoryRequest', 'job': 'HistoryJob', 'failed_job': 'HistoryJob',
            'result': 'HistoryJobResult', 'page_request': 'HistoryPageRequest',
            'viewport_query': 'HistoryViewportQuery', 'viewport_cursor': 'HistoryViewportCursor',
            'viewport_page': 'HistoryViewportPage',
        }
        for key, schema in names.items():
            with self.subTest(key=key):
                self.validator(schema).validate(self.fixture()[key])
        document = self.document()
        jsonschema.Draft202012Validator.check_schema(document)
        job = document['components']['schemas']['HistoryJob']
        self.assertEqual(job['properties']['source_boundary']['format'], 'uint64')
        self.assertIn('source_boundary', job['required'])

    def test_invalid_ranges_null_presence_and_occupancy_threshold_fail_schema_validation(self):
        fixture = self.fixture()
        cases = []
        for field, value in [('started_at_us', -(1 << 63) - 1), ('ended_at_us', 1 << 63),
                             ('min_frequency_hz', -1), ('max_frequency_hz', 1 << 63)]:
            changed = deepcopy(fixture['request'])
            changed['filters'][field] = value
            cases.append(('HistoryRequest', changed))
        for value in [-1, 1 << 64]:
            changed = deepcopy(fixture['job'])
            changed['source_boundary'] = value
            cases.append(('HistoryJob', changed))
        changed = deepcopy(fixture['job'])
        del changed['source_boundary']
        cases.append(('HistoryJob', changed))
        changed = deepcopy(fixture['request'])
        changed['threshold'] = None
        cases.append(('HistoryRequest', changed))
        changed = deepcopy(fixture['request'])
        changed['operation'] = 'summary'
        cases.append(('HistoryRequest', changed))
        for schema, value in cases:
            with self.subTest(schema=schema, value=value):
                with self.assertRaises(jsonschema.ValidationError):
                    self.validator(schema).validate(value)

    def test_baseline_rendered_bytes_and_support_map_remain_identical(self):
        from tools.contracts.generate import load_document, render
        for path, content in render(load_document(None)).items():
            self.assertEqual((ROOT / path).read_bytes(), content.encode(), path)
        document = self.document()
        baseline = json.loads((ROOT / 'contracts/generated/openapi-v1.json').read_text())
        self.assertTrue(all(document['components']['schemas'][name] == schema
                            for name, schema in baseline['components']['schemas'].items()))

    def test_unknown_integer_format_fails_before_publishing_any_generated_output(self):
        from tools.contracts.runtime_contract import load_extension
        extension = load_extension()
        extension['components']['schemas']['UnknownInteger'] = {
            'type': 'object', 'properties': {'value': {'type': 'integer', 'format': 'int128'}},
            'required': ['value']}
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'extension.json'
            source.write_text(json.dumps(extension))
            output = Path(folder) / 'not-created'
            result = subprocess.run([
                sys.executable, str(ROOT / 'tools/contracts/generate.py'), '--variant', 'rust-candidate',
                '--runtime-extension', str(source), '--output-root', str(output)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(output.exists())

    def test_unrepresentable_format_bound_or_unreviewed_union_fails_before_output(self):
        from tools.contracts.runtime_contract import load_extension
        for field in [
            {'type': 'integer', 'format': 'uint64', 'maximum': 1 << 64},
            {'type': 'integer', 'format': 'int64', 'minimum': -(1 << 63) - 1},
            {'anyOf': [{'$ref': '#/components/schemas/HistoryJobFailure'},
                       {'type': 'null'}, {'type': 'string'}]},
        ]:
            extension = load_extension()
            extension['components']['schemas']['Unreviewed'] = {
                'type': 'object', 'properties': {'value': field}, 'required': ['value']}
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / 'extension.json'
                source.write_text(json.dumps(extension))
                output = Path(folder) / 'not-created'
                result = subprocess.run([
                    sys.executable, str(ROOT / 'tools/contracts/generate.py'), '--variant', 'rust-candidate',
                    '--runtime-extension', str(source), '--output-root', str(output)],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(output.exists())

    def test_derived_response_schema_does_not_narrow_historical_source_ids_or_raw_units(self):
        page = self.fixture()['viewport_page']
        original_id = 'e\u0301/雪\n' * 512
        original_unit = 'uncalibrated raw unit\n' * 256
        page['rows'][0]['measurement_id'] = original_id
        page['rows'][0]['level_unit'] = original_unit
        page['next_cursor']['after']['id'] = original_id
        self.validator('HistoryViewportPage').validate(page)


if __name__ == '__main__':
    unittest.main()
