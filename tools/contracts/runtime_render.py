"""Generate the separately importable runtime SDK; baseline codecs stay unchanged."""
import hashlib
import json


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def _runtime_walker(template):
    walker = template[template.index('interface Schema '):template.index('export function decodeModel(')]
    # Baseline codec bytes are frozen. Reuse its actual strict parser/walker,
    # extending only the separately generated runtime schema interpretation.
    replacements = [
        ('interface Schema {', 'interface Schema {maximum?: number; anyOf?: readonly Schema[]; '),
        ('function walk(schema: Schema, value: JSONValue, encode: boolean): JSONValue {\n', '''function walk(schema: Schema, value: JSONValue, encode: boolean): JSONValue {
  if (schema.anyOf) {
    const reference=schema.anyOf.find(branch=>branch.$ref!==undefined);
    if (schema.anyOf.length!==2 || !reference || !schema.anyOf.some(branch=>branch.type==='null')) throw new TypeError('Unsupported runtime union');
    return value===null ? null : walk(reference,value,encode);
  }
'''),
        ("    if (schema.minimum!==undefined && value<BigInt(schema.minimum)) throw new TypeError('Integer below minimum');", '''    if (schema.format!==undefined) {
      const exact=BigInt(value as number | bigint);
      // Never convert a schema's rounded JavaScript maximum back into the
      // authority for a 64-bit range. These format endpoints are exact.
      if (schema.format==='int64') {
        if (exact < -9223372036854775808n || exact > 9223372036854775807n) throw new TypeError('Integer outside int64');
      } else if (schema.format==='uint64') {
        if (exact < 0n || exact > 18446744073709551615n) throw new TypeError('Integer outside uint64');
      } else throw new TypeError('Unsupported integer format');
    }
    if (schema.minimum!==undefined && value<BigInt(schema.minimum)) throw new TypeError('Integer below minimum');
    if (schema.maximum!==undefined && value>BigInt(schema.maximum)) throw new TypeError('Integer above maximum');
    if (typeof value==='number' && Object.is(value,-0)) value=0;'''),
    ]
    for before, after in replacements:
        if walker.count(before) != 1:
            raise ValueError('Strict baseline walker changed; review runtime extension')
        walker = walker.replace(before, after, 1)
    return walker


def render_runtime(document, extension, generator):
    document = json.loads(_canonical(document))
    extension = json.loads(_canonical(extension))
    schemas = {name: schema for name, schema in extension['components']['schemas'].items()
               if schema.get('type') == 'object' and 'properties' in schema}
    operations = []
    for path, methods in document['paths'].items():
        for method, operation in methods.items():
            operations.append({'operation_id': operation['operationId'], 'method': method.upper(), 'path': path,
                'query': [p['name'] for p in operation.get('parameters', []) if p['in']=='query'],
                'path_parameters': [p['name'] for p in operation.get('parameters', []) if p['in']=='path'],
                'max_request_bytes': operation.get('x-ori-max-request-bytes', operation.get('x-max-request-bytes')),
                'backends': operation['x-ori-backends'], 'features': operation['x-ori-feature-requirements'],
                'capabilities': operation['x-ori-capability-requirements'],
                'request_features': operation['x-ori-request-feature-requirements']})
    canonical = _canonical(document)
    outputs = {
        'contracts/generated/runtime-openapi-v1.json': canonical,
        'contracts/generated/runtime-manifest.json': _canonical({'extension_version': 1, 'variant': 'rust-candidate',
            'runtime_schema_sha256': hashlib.sha256(canonical.encode()).hexdigest(),
            'extension_sha256': hashlib.sha256(_canonical(extension).encode()).hexdigest(),
            'baseline_schema_sha256': document['x-ori-baseline-schema-sha256']}),
    }
    # Reuse the exact schema walker from the transport template with the same
    # Float64 class/parser/serializer. A second JSON codec would split markers.
    template = (generator.TEMPLATES/'typescript.ts').read_text()
    walker = _runtime_walker(template)
    ts = generator.ts_models(schemas, include_common=False)
    ts += "\nimport {Float64, stringifyJSON, normalizeUTC} from './codec.ts';\nimport {schemas as baselineSchemas} from './schema.ts';\n"
    ts += 'const schemas = {...baselineSchemas, ...' + _canonical(extension['components']['schemas']).rstrip() + '} as const;\n'
    ts += "const validUnicode = (text: string): void => { stringifyJSON(text); };\nconst put = (object: JSONObject, key: string, value: JSONValue): void => { Object.defineProperty(object,key,{value,enumerable:true,writable:true,configurable:true}); };\n"
    ts += walker
    for name in schemas:
        ts += f"export const decode{name} = (value: JSONValue): {name} => walk(schemas.{name} as unknown as Schema,value,false) as unknown as {name};\n"
        ts += f"export const encode{name} = (value: {name}): string => stringifyJSON(walk(schemas.{name} as unknown as Schema,value as unknown as JSONValue,true));\n"
    ts += "export {decodeSweepSummaryPage, encodeSweepSummaryPage} from './index.ts';\nexport type {SweepSummaryPage} from './models.ts';\n"
    ts += 'export const runtimeOperations = ' + _canonical(operations).rstrip() + ' as const;\n'
    ts += '''
/** Feature requirements describe the Rust candidate. Legacy operations retain
 * their original Python availability. Request conditions apply to the given body. */
export function selectRuntimeOperations(backend: string, features: readonly string[] = [], capabilities: readonly string[] = [], request: JSONObject = {}): ReadonlyArray<(typeof runtimeOperations)[number]> {
  if (backend !== 'python-legacy' && backend !== 'rust-candidate') throw new TypeError('Unknown contract backend');
  return runtimeOperations.filter(operation => (operation.backends as readonly string[]).includes(backend) &&
    (backend === 'python-legacy' || operation.features.every(feature => features.includes(feature)) &&
     operation.capabilities.every(capability => capabilities.includes(capability)) &&
     operation.request_features.every(condition => request[condition.field] !== condition.equals || features.includes(condition.feature))));
}
'''
    outputs['contracts/generated/typescript/runtime.ts'] = ts
    rust = generator.rust_models(schemas, include_common=False)
    rust += '\npub use crate::{SweepSummary, SweepSummaryPage};\n'
    rust += '''
#[derive(Debug, Clone, Copy)]
pub struct RequestFeature { pub field: &'static str, pub equals: bool, pub feature: &'static str }
#[derive(Debug, Clone, Copy)]
pub struct RuntimeOperation {
    pub operation_id: &'static str, pub method: &'static str, pub path: &'static str,
    pub backends: &'static [&'static str], pub features: &'static [&'static str],
    pub capabilities: &'static [&'static str], pub request_features: &'static [RequestFeature],
    pub query: &'static [&'static str], pub path_parameters: &'static [&'static str], pub max_request_bytes: Option<usize>,
}
pub const OPERATIONS: &[RuntimeOperation] = &[
'''
    for op in operations:
        conditions = ','.join('RequestFeature { field: '+json.dumps(c['field'])+', equals: '+str(c['equals']).lower()+', feature: '+json.dumps(c['feature'])+' }' for c in op['request_features'])
        fields = ','.join(key+': '+('&'+json.dumps(op[key]) if isinstance(op[key], list) else json.dumps(op[key])) for key in ('operation_id', 'method', 'path', 'backends', 'features', 'capabilities', 'query', 'path_parameters'))
        fields += ', max_request_bytes: '+('None' if op['max_request_bytes'] is None else f"Some({op['max_request_bytes']})")
        rust += 'RuntimeOperation { '+fields+', request_features: &['+conditions+'] },\n'
    rust += '''];
pub fn select_operations(backend: &str, features: &[&str], capabilities: &[&str], request: &JSONObject) -> Result<Vec<&'static RuntimeOperation>, crate::CodecError> {
    if !["python-legacy", "rust-candidate"].contains(&backend) {
        return Err(crate::CodecError::Invalid("Unknown contract backend".into()));
    }
    Ok(OPERATIONS.iter().filter(|operation| operation.backends.contains(&backend) &&
        (backend == "python-legacy" || operation.features.iter().all(|feature| features.contains(feature)) &&
         operation.capabilities.iter().all(|capability| capabilities.contains(capability)) &&
         operation.request_features.iter().all(|condition| request.get(condition.field).and_then(serde_json::Value::as_bool) != Some(condition.equals) || features.contains(&condition.feature)))).collect())
}
'''
    outputs['contracts/generated/rust/runtime.rs'] = generator.format_rust(rust)
    swift = generator.swift_models(schemas, include_common=False)
    swift += '''
public struct RuntimeRequestFeature: Sendable { public let field: String; public let equals: Bool; public let feature: String }
public struct RuntimeOperationDescriptor: Sendable {
    public let operationId: String; public let method: String; public let path: String
    public let backends: [String]; public let features: [String]; public let capabilities: [String]
    public let requestFeatures: [RuntimeRequestFeature]
    public let query: [String]; public let pathParameters: [String]; public let maxRequestBytes: Int?
}
public let runtimeOperations: [RuntimeOperationDescriptor] = [
'''
    for op in operations:
        conditions = ','.join('RuntimeRequestFeature(field: '+json.dumps(c['field'])+', equals: '+str(c['equals']).lower()+', feature: '+json.dumps(c['feature'])+')' for c in op['request_features'])
        values = ','.join(('operationId' if key=='operation_id' else key)+': '+json.dumps(op[key]) for key in ('operation_id', 'method', 'path', 'backends', 'features', 'capabilities'))
        swift += 'RuntimeOperationDescriptor('+values+', requestFeatures: ['+conditions+'], query: '+json.dumps(op['query'])+', pathParameters: '+json.dumps(op['path_parameters'])+', maxRequestBytes: '+('nil' if op['max_request_bytes'] is None else str(op['max_request_bytes']))+'),\n'
    swift += ''']
public func selectRuntimeOperations(backend: String, features: Set<String> = [], capabilities: Set<String> = [], request: JSONObject = [:]) throws -> [RuntimeOperationDescriptor] {
    guard ["python-legacy", "rust-candidate"].contains(backend) else { throw ContractError.invalid("Unknown contract backend") }
    return runtimeOperations.filter { operation in
        operation.backends.contains(backend) && (backend == "python-legacy" ||
            operation.features.allSatisfy { features.contains($0) } &&
            operation.capabilities.allSatisfy { capabilities.contains($0) } &&
            operation.requestFeatures.allSatisfy { condition in request[condition.field] != .bool(condition.equals) || features.contains(condition.feature) })
    }
}
'''
    outputs['contracts/generated/swift/Sources/ORIContracts/Runtime.swift'] = swift
    return outputs
