"""The public capability schema distinguishes live acquisition from replay."""
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from radioinspector.web.openapi import document


class CoreExecutionContractTests(unittest.TestCase):
    def test_capabilities_accept_live_and_legacy_but_reject_unknown_modes(self):
        spec = document()
        self.assertTrue('CoreCapabilities' in spec['components']['schemas'],
                        'Published capabilities must describe execution modes')
        fixture = json.loads((Path(__file__).resolve().parents[1] /
                              'contracts/fixtures/core-execution-v1.json').read_text())
        validator = Draft202012Validator({
            '$ref': '#/components/schemas/CoreCapabilities', 'components': spec['components']})
        for payload in fixture['valid']:
            with self.subTest(payload=payload):
                validator.validate(payload)
        for mode in fixture['invalid_modes']:
            with self.subTest(mode=mode):
                self.assertTrue(list(validator.iter_errors({'features': {}, 'candidate': {'mode': mode}})))
        response = spec['paths']['/api/v1/capabilities']['get']['responses']['200']['content']['application/json']['schema']
        self.assertEqual(response['allOf'][1]['properties']['data'],
                         {'$ref': '#/components/schemas/CoreCapabilities'})

    def test_candidate_declares_optional_owner_namespace_without_changing_legacy_paths(self):
        from tools.contracts.runtime_contract import compose_runtime_document, load_extension
        baseline = document()
        candidate = compose_runtime_document(baseline, load_extension())
        scope = candidate['x-ori-runtime-owner-scope']
        self.assertEqual(scope['prefix'], '/api/v1/instances/{runtime_instance_id}')
        self.assertEqual(scope['feature'], 'runtime_owner_scoped_requests')
        self.assertEqual(scope['owner_mismatch_status'], 409)
        self.assertTrue(scope['original_paths_preserved'])
        self.assertNotIn('x-ori-runtime-owner-scope', baseline)
        for path in baseline['paths']:
            self.assertIn(path, candidate['paths'])
