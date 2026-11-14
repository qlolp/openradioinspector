"""Additive backend selection, schema validation and generation behavior."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / 'contracts/generated/openapi-v1.json'
SOURCE = ROOT / 'contracts/runtime-extension-v1.json'


class RuntimeContractTests(unittest.TestCase):
    def composer(self):
        return importlib.import_module('tools.contracts.runtime_contract')

    def documents(self):
        return json.loads(BASELINE.read_text()), json.loads(SOURCE.read_text())

    def test_runtime_variant_generates_additive_artifacts_without_replacing_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, str(ROOT/'tools/contracts/generate.py'),
                '--variant', 'rust-candidate', '--output-root', folder], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            target = Path(folder)
            runtime = json.loads((target/'contracts/generated/runtime-openapi-v1.json').read_text())
            self.assertEqual(runtime['x-ori-contract-variant'], 'rust-candidate')
            self.assertTrue((target/'contracts/generated/typescript/runtime.ts').is_file())
            self.assertTrue((target/'contracts/generated/swift/Sources/ORIContracts/Runtime.swift').is_file())
            self.assertTrue((target/'contracts/generated/rust/runtime.rs').is_file())
            self.assertFalse((target/'contracts/generated/openapi-v1.json').exists())
            self.assertFalse((target/'crates/ori-contracts/src/models.rs').exists())

    def test_composition_keeps_baseline_schemas_paths_and_rendered_bytes_unchanged(self):
        from tools.contracts.generate import render
        baseline, extension = self.documents()
        original = deepcopy(baseline)
        before = render(baseline)
        runtime = self.composer().compose_runtime_document(baseline, extension)
        self.assertEqual(baseline, original)
        self.assertEqual(render(baseline), before)
        for name, content in before.items():
            self.assertEqual((ROOT/name).read_bytes(), content.encode())
        for name, schema in baseline['components']['schemas'].items():
            self.assertEqual(runtime['components']['schemas'][name], schema)
        for path, methods in baseline['paths'].items():
            for method, operation in methods.items():
                self.assertEqual(runtime['paths'][path][method]['operationId'], operation['operationId'])

    def test_complete_document_has_unique_operations_resolved_refs_and_path_parameters(self):
        baseline, extension = self.documents()
        runtime = self.composer().compose_runtime_document(baseline, extension)
        ids, routes = set(), set()
        def walk(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    target = runtime
                    self.assertTrue(value['$ref'].startswith('#/'))
                    for part in value['$ref'][2:].split('/'):
                        target = target[part.replace('~1', '/').replace('~0', '~')]
                for item in value.values(): walk(item)
            elif isinstance(value, list):
                for item in value: walk(item)
        walk(runtime)
        for path, methods in runtime['paths'].items():
            for method, operation in methods.items():
                self.assertNotIn(operation['operationId'], ids)
                ids.add(operation['operationId'])
                route = (re.sub(r'\{[^}]+\}', '{}', path), method)
                self.assertNotIn(route, routes)
                routes.add(route)
                expected = set(re.findall(r'\{([^}]+)\}', path))
                actual = {p['name'] for p in operation.get('parameters', []) if p['in']=='path' and p.get('required') is True}
                self.assertEqual(actual, expected, (path, method))

    def test_new_routes_and_unmapped_legacy_routes_have_truthful_backend_support(self):
        baseline, extension = self.documents()
        runtime = self.composer().compose_runtime_document(baseline, extension)
        security = runtime['paths']['/api/v1/security/status']['get']
        self.assertEqual(security['x-ori-backends'], ['rust-candidate'])
        self.assertEqual(runtime['paths']['/api/v1/sessions/{id}/sweep-summaries']['get']['operationId'],
                         'get_api_v1_sessions_id_sweep_summaries')
        unsupported = next(op for methods in runtime['paths'].values() for op in methods.values()
                           if op['operationId']=='get_api_v1_monitoring_projects_project_id_history_sweep_id_comparison')
        self.assertNotIn('rust-candidate', unsupported['x-ori-backends'])
        self.assertEqual(unsupported['x-ori-runtime-support'], 'compatibility-target')
        self.assertNotIn('/api/v1/security/status', baseline['paths'])

    def test_unknown_extension_version_backend_or_extension_key_fails_closed(self):
        baseline, extension = self.documents()
        for key, value in [('extension', 'unknown'), ('extension_version', 2),
                           ('backends', ['unknown']), ('surprise', True)]:
            changed = deepcopy(extension)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.composer().compose_runtime_document(baseline, changed)

    def test_invalid_variant_is_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'not-created'
            result = subprocess.run([sys.executable, str(ROOT/'tools/contracts/generate.py'),
                '--variant', 'unknown', '--output-root', str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.exists())

    def test_baseline_cli_does_not_ignore_an_explicit_invalid_extension(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'extension.json'
            source.write_text(json.dumps({'extension':'unknown','extension_version':1,'backends':['rust-candidate']}))
            target=root/'not-created'
            result=subprocess.run([sys.executable,str(ROOT/'tools/contracts/generate.py'),
                '--variant','baseline','--runtime-extension',str(source),'--output-root',str(target)],capture_output=True,text=True)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertFalse(target.exists())

    def test_legacy_variant_remains_identical_but_still_rejects_invalid_extensions(self):
        baseline, extension = self.documents()
        self.assertEqual(self.composer().compose_runtime_document(baseline, extension, 'python-legacy'), baseline)
        extension['paths']['/api/v1/security/status']['get']['responses']['200']['content']['application/json']['schema']={'$ref':'#/components/schemas/Missing'}
        with self.assertRaises(ValueError):
            self.composer().compose_runtime_document(baseline, extension, 'python-legacy')

    def test_candidate_authentication_is_explicit_and_baseline_remains_unchanged(self):
        baseline, extension = self.documents()
        runtime = self.composer().compose_runtime_document(baseline, extension)
        self.assertEqual(runtime['security'], [{'nativeBearer': []}, {'browserSession': []}])
        self.assertEqual(runtime['paths']['/api/v1/security/status']['get']['security'], [])
        self.assertEqual(runtime['paths']['/api/v1/security/pair']['post']['security'], [])
        self.assertEqual(runtime['paths']['/api/v1/security/browser-session']['post']['security'], [{'nativeBearer': []}])
        self.assertNotIn('security', baseline)

    def test_colliding_operation_or_normalized_route_and_unresolved_ref_or_parameter_are_rejected(self):
        baseline, extension = self.documents()
        for mutation in ('id', 'route', 'ref', 'parameter', 'baseline_schema', 'unknown_support'):
            changed = deepcopy(extension)
            operation = changed['paths']['/api/v1/security/status']['get']
            if mutation=='id': operation['operationId']='get_api_v1_sessions'
            elif mutation=='route':
                changed['paths']['/api/v1/sessions/{session_id}/sweeps']={'get':deepcopy(operation)}
            elif mutation=='ref':
                operation['responses']['200']['content']['application/json']['schema']={'$ref':'#/components/schemas/Missing'}
            elif mutation=='parameter':
                operation['parameters']=[{'name':'id','in':'path','required':True,'schema':{'type':'string'}}]
            elif mutation=='baseline_schema': changed['components']['schemas']['Sweep']={'type':'object','properties':{}}
            else: changed['baseline_support']['missing_operation']={'features':[], 'capabilities':[]}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.composer().compose_runtime_document(baseline, changed)

    def test_common_runtime_fixtures_satisfy_schemas_and_reuse_summary_type(self):
        import jsonschema
        baseline, extension = self.documents()
        runtime = self.composer().compose_runtime_document(baseline, extension)
        jsonschema.Draft202012Validator.check_schema(runtime)
        fixtures=json.loads((ROOT/'contracts/fixtures/runtime-v1.json').read_text())
        for key, schema in [('security_status','SecurityStatus'), ('browser_pairing','BrowserPairing'),
                            ('native_pairing','NativePairing'), ('websocket_descriptor','MeasurementWebSocket'),
                            ('summary_page','SweepSummaryPage')]:
            validator=jsonschema.Draft202012Validator({'$ref':f'#/components/schemas/{schema}','components':runtime['components']})
            validator.validate(fixtures[key])
        self.assertEqual(runtime['components']['schemas']['SweepSummaryPage'],baseline['components']['schemas']['SweepSummaryPage'])


if __name__=='__main__': unittest.main()
