#!/usr/bin/env python3
"""Validate a supplied Core binary on an owned, synthetic loopback node.

No authentication provisioning, physical inputs or serial access. Every run
retains its fresh archive, raw responses, process log and success/failure report.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.modernization.api_parity import NodeSpec, OwnedNode


def probe(binary, directory, require_shared=False):
    import jsonschema
    runtime = json.loads((ROOT/'contracts/generated/runtime-openapi-v1.json').read_text())
    fixture = json.loads((ROOT/'contracts/fixtures/measurement-v1.json').read_text())
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    with binary.open('rb') as executable:
        binary_sha256 = hashlib.file_digest(executable, 'sha256').hexdigest()
    report = {'status': 'failed', 'binary_sha256': binary_sha256,
              'checks': [], 'authentication_positive_tested': False, 'hardware_tested': False}
    responses = {}
    def validate(schema, value):
        jsonschema.Draft202012Validator({**schema, 'components': runtime['components']}).validate(value)
    def record(node, name, method, path, payload=None, expected=200):
        response = node.request(method, path, payload)
        value = json.loads(response.body)
        responses[name] = {'status': response.status, 'body': value}
        if response.status != expected:
            raise AssertionError(f'{name}: expected HTTP {expected}, got {response.status}')
        return value
    try:
        with OwnedNode(NodeSpec('core', binary, ROOT), directory/'node') as node:
            status = record(node, 'security_status', 'GET', '/api/v1/security/status')
            validate({'$ref': '#/components/schemas/SecurityStatusEnvelope'}, status)
            if status['data'] != {'mode': 'legacy_loopback', 'pairing_required': False}:
                raise AssertionError('Owned fixture must stay in nonarmed loopback mode')
            pair = record(node, 'pair_loopback', 'POST', '/api/v1/security/pair',
                          {'pairing_code': 'synthetic-not-a-secret', 'label': 'Contract probe', 'client': 'native'}, 409)
            validate({'$ref': '#/components/schemas/ErrorEnvelope'}, pair)
            if pair['error']['code'] != 'pairing_not_required': raise AssertionError('Loopback pairing semantics changed')
            capabilities = record(node, 'capabilities', 'GET', '/api/v1/capabilities')['data']['features']
            validate({'$ref': '#/components/schemas/MeasurementWebSocket'}, capabilities['measurement_binary_stream'])
            if capabilities['control_task_execution'] is not False or capabilities['control_task_scheduler'] is not False:
                raise AssertionError('This candidate probe must not enable task execution')
            task = record(node, 'task_execution_refused', 'POST', '/api/v1/task-scheduler', {'enabled': True}, 503)
            validate({'$ref': '#/components/schemas/ErrorEnvelope'}, task)
            if task['error']['code'] != 'task_execution_unavailable': raise AssertionError('Task execution did not fail closed')
            record(node, 'create', 'POST', '/api/v1/sessions', fixture['session'], 201)
            base = '/api/v1/sessions/'+quote(fixture['session']['id'], safe='')
            record(node, 'source', 'POST', base+'/sweeps', fixture['sweep'], 201)
            summary = record(node, 'summary', 'GET', base+'/sweep-summaries?limit=1')
            validate({'allOf': [{'$ref': '#/components/schemas/Envelope'},
                      {'properties': {'data': {'$ref': '#/components/schemas/SweepSummaryPage'}}}]}, summary)
            data = summary['data']
            if len(data['items']) != 1 or data['next_cursor'] is not None or struct.pack('>d', data['items'][0]['peak_level']).hex() != '8000000000000000':
                raise AssertionError('Original summary count, explicit null or float64 negative zero changed')
            actual = record(node, 'openapi', 'GET', '/api/v1/openapi.json')
            if require_shared and json.dumps(actual, sort_keys=True) != json.dumps(runtime, sort_keys=True):
                raise AssertionError('Core has not embedded the canonical shared runtime OpenAPI')
            report['checks'] = ['security_status', 'loopback_pairing_refusal', 'websocket_descriptor',
                                'task_execution_refusal', 'summary_schema_exact_negative_zero', 'graceful_owned_join']
            if require_shared: report['checks'].append('canonical_shared_openapi')
        report['status'] = 'passed'
    except BaseException as error:
        report['error'] = str(error)
        raise
    finally:
        (directory/'responses.json').write_text(json.dumps(responses, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
        (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-binary', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--require-shared-openapi', action='store_true')
    args = parser.parse_args()
    try:
        report = probe(args.core_binary.resolve(strict=True), args.report_dir, args.require_shared_openapi)
    except (OSError, ValueError, AssertionError, RuntimeError) as error:
        print(f'Runtime contract probe failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__': raise SystemExit(main())
