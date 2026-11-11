"""Reviewed additive contract composition; importing this never starts a server."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

BACKENDS = ('python-legacy', 'rust-candidate')
METHODS = frozenset(('get', 'post', 'put', 'patch', 'delete', 'head', 'options'))
SOURCE = Path(__file__).resolve().parents[2] / 'contracts/runtime-extension-v1.json'


def load_extension(source=SOURCE):
    return json.loads(Path(source).read_text(encoding='utf-8'))


def _normalized(path):
    return re.sub(r'\{[^}]+\}', '{}', path)


def validate_document(document):
    identifiers, routes = set(), set()
    for path, methods in document['paths'].items():
        if not path.startswith('/api/v1/'):
            raise ValueError('Runtime paths must use /api/v1/')
        for method, operation in methods.items():
            if method not in METHODS or not isinstance(operation, dict):
                raise ValueError('Unsupported runtime path member')
            identifier = operation.get('operationId')
            if not isinstance(identifier, str) or not identifier or identifier in identifiers:
                raise ValueError('Missing or duplicate operationId')
            identifiers.add(identifier)
            route = (_normalized(path), method)
            if route in routes:
                raise ValueError('Duplicate normalized operation path')
            routes.add(route)
            parameters = operation.get('parameters', [])
            path_parameters = [p for p in parameters if p.get('in') == 'path']
            expected = set(re.findall(r'\{([^}]+)\}', path))
            actual = {p['name'] for p in path_parameters if p.get('required') is True}
            if actual != expected or len(path_parameters) != len(expected):
                raise ValueError('Unresolved or duplicate path parameter')
            names = [(p.get('in'), p.get('name')) for p in parameters]
            if len(names) != len(set(names)):
                raise ValueError('Duplicate operation parameter')
    def references(value):
        if isinstance(value, dict):
            if '$ref' in value:
                ref = value['$ref']
                if not isinstance(ref, str) or not ref.startswith('#/'):
                    raise ValueError('Only resolved local contract references are allowed')
                target = document
                try:
                    for part in ref[2:].split('/'):
                        target = target[part.replace('~1', '/').replace('~0', '~')]
                except (KeyError, TypeError) as error:
                    raise ValueError('Unresolved contract reference: ' + ref) from error
            for item in value.values(): references(item)
        elif isinstance(value, list):
            for item in value: references(item)
    references(document)
    schemes = document.get('components', {}).get('securitySchemes', {})
    security = [document.get('security', [])] + [op.get('security', []) for methods in document['paths'].values() for op in methods.values()]
    for alternatives in security:
        if not isinstance(alternatives, list) or any(not isinstance(item, dict) or set(item)-set(schemes) for item in alternatives):
            raise ValueError('Unresolved security scheme')


def _policy(value):
    if not isinstance(value, dict) or set(value) - {'features', 'capabilities', 'request_features'}:
        raise ValueError('Unknown runtime support policy')
    for key in ('features', 'capabilities'):
        items = value.get(key, [])
        if not isinstance(items, list) or any(not isinstance(v, str) or not v for v in items) or len(items) != len(set(items)):
            raise ValueError('Invalid runtime feature/capability requirements')
    for item in value.get('request_features', []):
        if not isinstance(item, dict) or set(item) != {'field', 'equals', 'feature'} or not isinstance(item['field'], str) or type(item['equals']) is not bool or not isinstance(item['feature'], str):
            raise ValueError('Invalid request feature requirement')
    return value


def _tag(operation, backends, policy, support):
    operation['x-ori-backends'] = backends
    operation['x-ori-runtime-support'] = support
    operation['x-ori-feature-requirements'] = policy.get('features', [])
    operation['x-ori-capability-requirements'] = policy.get('capabilities', [])
    operation['x-ori-request-feature-requirements'] = policy.get('request_features', [])


def compose_runtime_document(baseline, extension, variant='rust-candidate'):
    if variant not in BACKENDS:
        raise ValueError('Unknown contract backend variant')
    allowed = {'extension', 'extension_version', 'backends', 'description', 'baseline_support', 'components', 'paths'}
    if set(extension) - allowed or extension.get('extension') != 'openradioinspector-runtime' or type(extension.get('extension_version')) is not int or extension['extension_version'] != 1 or extension.get('backends') != ['rust-candidate']:
        raise ValueError('Unknown runtime extension, version, backend or key')
    validate_document(baseline)
    support = extension.get('baseline_support', {})
    if not isinstance(support, dict): raise ValueError('Invalid baseline support map')
    known = {op['operationId'] for methods in baseline['paths'].values() for op in methods.values()}
    if set(support) - known: raise ValueError('Unknown baseline operation in support map')
    for policy in support.values(): _policy(policy)
    document = deepcopy(baseline)
    for methods in document['paths'].values():
        for operation in methods.values():
            policy = support.get(operation['operationId'])
            _tag(operation, ['python-legacy'] + (['rust-candidate'] if policy is not None else []),
                 policy or {}, 'feature-gated' if policy is not None else 'compatibility-target')
    components = extension.get('components', {})
    if set(components) - {'schemas', 'securitySchemes'}: raise ValueError('Unknown extension component')
    for section, entries in components.items():
        existing = document.setdefault('components', {}).setdefault(section, {})
        if set(existing) & set(entries): raise ValueError('Extension cannot replace baseline components')
        existing.update(deepcopy(entries))
    for path, methods in extension.get('paths', {}).items():
        for method, supplied in methods.items():
            operation = deepcopy(supplied)
            policy = operation.pop('x-ori-support', None)
            if policy is None or any(key.startswith('x-ori-') for key in operation):
                raise ValueError('New operation requires one explicit known support policy')
            _tag(operation, ['rust-candidate'], _policy(policy), 'feature-gated')
            if method in document.get('paths', {}).get(path, {}):
                raise ValueError('Extension cannot replace baseline operations')
            document['paths'].setdefault(path, {})[method] = operation
    document['security'] = [{'nativeBearer': []}, {'browserSession': []}]
    document['x-ori-authentication-modes'] = {
        'lan_tls': 'All API operations require a validated credential except GET security/status and POST security/pair. Pair requires trusted TLS/Host/Origin and a locally opened single-use window. Cookie mutations require Origin.',
        'legacy_loopback': 'API credential authentication is disabled; existing mutation-origin checks still apply.',
        'browser_session': 'Valid native Bearer only; cookie credentials cannot mint a browser session.',
        'revocation': 'Revocation invalidates the native credential and browser sessions.',
        'internal_runtime': 'Separate authenticated loopback IPC is outside this public API contract.'}
    validate_document(document)
    if variant == 'python-legacy': return deepcopy(baseline)
    document['x-ori-contract-variant'] = variant
    document['x-ori-baseline-schema-sha256'] = hashlib.sha256((json.dumps(baseline, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()).hexdigest()
    document['x-ori-extension-version'] = 1
    document['x-ori-support-policy'] = 'Only listed backends plus satisfied feature/capability requirements indicate support. Compatibility targets are not implementation claims. Request feature conditions must also be checked.'
    return document
