"""The served contract must describe actual existing REST responses."""
import http.client
import json
import socket
import threading
import unittest

from jsonschema import Draft202012Validator

from radioinspector.web.server import create_server
from tests.test_platform import session_payload


class OpenAPIContractTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server(host='127.0.0.1', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, None if payload is None else json.dumps(payload),
                               {} if payload is None else {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def contract(self):
        status, spec = self.request('GET', '/api/v1/openapi.json')
        self.assertEqual(status, 200, 'The additive public OpenAPI resource is missing')
        self.assertEqual(spec['openapi'], '3.1.0')
        return spec

    def validate_response(self, spec, path, method, status, response):
        schema = spec['paths'][path][method]['responses'][str(status)]['content']['application/json']['schema']
        document = {'$defs': spec['components']['schemas'], **schema}
        # Keep local references resolvable without network access.
        document['components'] = spec['components']
        Draft202012Validator(document).validate(response)

    def test_reading_contract_is_side_effect_free_and_advertised(self):
        before = self.server.session_service.events()
        spec = self.contract()
        Draft202012Validator.check_schema({'$defs': spec['components']['schemas']})
        _, capabilities = self.request('GET', '/api/v1/capabilities')
        self.assertTrue(capabilities['data']['features']['openapi_v1'])
        self.assertEqual(self.server.session_service.events(), before)

    def test_graceful_stop_request_and_status_match_contract(self):
        spec = self.contract()
        _, capabilities = self.request('GET', '/api/v1/capabilities')
        self.assertTrue(capabilities['data']['features']['graceful_device_stop'])
        _, created = self.request('POST', '/api/v1/sessions', session_payload())
        session = created['data']
        templates = ('/api/v1/sessions/{id}/workers/stop',
                     '/api/v1/sessions/{id}/streams/{stream_id}/stop')
        for template in templates:
            operation = spec['paths'][template]['post']
            self.assertFalse(operation['requestBody']['required'])
            schema = operation['requestBody']['content']['application/json']['schema']
            validator = Draft202012Validator(schema)
            for payload in ({}, {'mode': 'graceful'}, {'mode': 'force'}):
                validator.validate(payload)
                path = template.replace('{id}', session['id']).replace('{stream_id}', session['streams'][0]['id'])
                status, response = self.request('POST', path, payload)
                self.assertEqual(status, 200, response)
                self.validate_response(spec, template, 'post', status, response)
            self.assertFalse(validator.is_valid({'mode': 'unknown'}))
            status, _ = self.request('POST', path, {'mode': 'unknown'})
            self.assertEqual(status, 400)

    def test_session_and_measurement_response_shapes_match_contract(self):
        spec = self.contract()
        status, session = self.request('POST', '/api/v1/sessions', session_payload())
        self.assertEqual(status, 201)
        self.validate_response(spec, '/api/v1/sessions', 'post', 201, session)
        identifier = session['data']['id']
        _, result = self.request('GET', f'/api/v1/sessions/{identifier}')
        self.validate_response(spec, '/api/v1/sessions/{id}', 'get', 200, result)
        _, sweeps = self.request('GET', f'/api/v1/sessions/{identifier}/sweeps')
        self.validate_response(spec, '/api/v1/sessions/{id}/sweeps', 'get', 200, sweeps)

    def test_documented_operations_have_unique_ids_and_resolved_parameters(self):
        spec = self.contract()
        identifiers = []
        for path, value in spec['paths'].items():
            for method, operation in value.items():
                if method not in ('get', 'post'):
                    continue
                identifiers.append(operation['operationId'])
                params = {parameter['name'] for parameter in operation.get('parameters', [])
                          if parameter['in'] == 'path' and parameter.get('required')}
                import re
                self.assertEqual(params, set(re.findall(r'\{([^}]+)\}', path)), path)
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for required in ('/api/v1/events', '/api/v1/monitoring-projects/{project_id}/history',
                         '/api/v1/sessions/{id}/streams/{stream_id}/instrument/operations'):
            self.assertIn(required, spec['paths'])

    def test_documented_body_limits_match_actual_rejection_boundaries(self):
        spec = self.contract()
        for suffix in ('sweep-quality', 'sweep-context', 'signal-list/import'):
            template = '/api/v1/sessions/{id}/' + suffix
            budget = spec['paths'][template]['post']['x-ori-max-request-bytes']
            body = b'{}' + b' '*(budget-2)
            path = '/api/v1/sessions/absent/' + suffix
            with self.subTest(resource=suffix), socket.create_connection(self.server.server_address, timeout=5) as connection:
                packet = (f'POST {path} HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                          f'Content-Length: {budget}\r\n\r\n').encode() + body
                connection.sendall(packet)
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertNotEqual(response.status, 413, 'Documented maximum exceeds the real reader budget')
                response.read()

    def test_calibrated_field_schema_validates_the_actual_request_and_result(self):
        from copy import deepcopy
        from pathlib import Path
        from radioinspector.contracts import to_primitive
        from tests.test_sweep_comparison import create_session
        from tests.test_field_strength_rms import source
        spec = self.contract()
        path = '/api/v1/sessions/{id}/field-strength-rms-v2'
        operation = spec['paths'][path]['post']
        self.assertEqual(operation['x-ori-max-request-bytes'], 65536)
        self.assertEqual(operation['x-ori-body-deadline-seconds'], 10)
        schema = operation['requestBody']['content']['application/json']['schema']
        validator = Draft202012Validator({'components': spec['components'], **schema})
        fixture = json.loads((Path(__file__).parent / 'fixtures/field-strength-rms-v2.json').read_text())
        session = create_session(self.server.session_service)
        sweep = source()
        sweep.session_id = session.id
        sweep.stream_id = session.streams[0].id
        self.server.session_service.ingest_sweep(to_primitive(sweep))
        body = fixture['request']
        body['sweep_id'] = sweep.id
        validator.validate(body)
        for mutate in ('extra', 'fractional_hz', 'missing_uncertainty', 'unknown_condition'):
            bad = deepcopy(body)
            if mutate == 'extra': bad['calibration']['unexpected'] = True
            elif mutate == 'fractional_hz': bad['frequency_hz'] = 1.5
            elif mutate == 'missing_uncertainty': del bad['calibration']['points'][0]['uncertainty_db']
            else: bad['calibration']['detector'] = 'PEAK'
            self.assertFalse(validator.is_valid(bad), mutate)
        for uncertainty in (3.0, None):
            if uncertainty is None:
                for point in body['calibration']['points']: point['uncertainty_db'] = None
            status, result = self.request('POST', path.replace('{id}', session.id), body)
            self.assertEqual(status, 200, result)
            self.validate_response(spec, path, 'post', status, result)
            self.assertEqual(result['data']['uncertainty_db'], uncertainty)

    def test_calibrated_field_response_keeps_existing_long_session_and_stream_ids(self):
        from radioinspector.contracts import to_primitive
        from tests.test_field_strength_rms import profile, source
        session_id, stream_id = 's' * 513, 'r' * 513
        session = self.server.session_service.create_session({'id': session_id, 'name': 'Long existing IDs',
            'streams': [{'id': stream_id, 'device': 'fixture', 'role': 'spectrum_analyzer', 'level_unit': 'dBm'}]})
        sweep = source(); sweep.session_id = session.id; sweep.stream_id = stream_id
        self.server.session_service.ingest_sweep(to_primitive(sweep))
        path = '/api/v1/sessions/{id}/field-strength-rms-v2'
        status, response = self.request('POST', path.replace('{id}', session.id),
            {'sweep_id': sweep.id, 'frequency_hz': 200, 'calibration': profile()})
        self.assertEqual(status, 200, response)
        self.assertEqual((response['data']['source']['session_id'], response['data']['source']['stream_id']),
                         (session_id, stream_id))
        self.validate_response(self.contract(), path, 'post', status, response)
