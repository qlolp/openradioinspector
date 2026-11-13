import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import './history_runtime.test.ts';
import {parseJSON, stringifyJSON, decodeSweep, decodeReceiverObservation, normalizeUTC, encodePathSegment, operationPath, encodeSweep, decodeEnvelope, decodeErrorEnvelope} from '../generated/typescript/index.ts';
import {decodeFieldStrengthRequest, decodeFieldStrengthResult, encodeFieldStrengthResult} from '../generated/typescript/index.ts';
import {decodeCoreCapabilities, encodeCoreCapabilities} from '../generated/typescript/index.ts';
import {decodeSecurityStatus, encodeSecurityStatus, decodeBrowserPairing, decodeNativePairing,
    decodeMeasurementWebSocket, encodeMeasurementWebSocket, decodeSweepSummaryPage,
    selectRuntimeOperations, runtimeOperations} from '../generated/typescript/runtime.ts';

const fixtureText = readFileSync(new URL('../fixtures/measurement-v1.json', import.meta.url), 'utf8');
test('core execution modes preserve legacy capabilities and unknown metadata', () => {
    const fixture = parseJSON(readFileSync(new URL('../fixtures/core-execution-v1.json', import.meta.url), 'utf8')) as any;
    for (const payload of fixture.valid) {
        assert.deepEqual(parseJSON(encodeCoreCapabilities(decodeCoreCapabilities(payload))), payload);
    }
    for (const mode of fixture.invalid_modes) {
        assert.throws(() => decodeCoreCapabilities({features: {}, candidate: {mode}}));
    }
});
test('calibrated field shared fixture preserves source, calibration and nullable uncertainty', () => {
    const fixture=parseJSON(readFileSync(new URL('../../tests/fixtures/field-strength-rms-v2.json',import.meta.url),'utf8')) as any;
    const request=decodeFieldStrengthRequest(fixture.request);
    assert.equal(request.frequency_hz,200); assert.equal(request.calibration.measurement_chain_id,'SYNTHETIC-receiver-antenna-cable-preamp-v1');
    const result=decodeFieldStrengthResult(fixture.result);
    assert.equal(result.value_dbuv_per_m,55); assert.equal(result.value,0.000562341325190349);
    assert.equal(result.uncertainty_db,3); assert.equal(result.source.id,'sweep-0');
    assert.equal(result.calibration.sha256,'f049d2a41686fd59a002637a5cde735d3843395308073a74ef4689731eb5b5b6');
    result.uncertainty_db=null;
    assert.equal(decodeFieldStrengthResult(parseJSON(encodeFieldStrengthResult(result))).uncertainty_db,null);
    delete fixture.result.uncertainty_db;
    assert.throws(()=>decodeFieldStrengthResult(fixture.result),/uncertainty_db/);
    assert.throws(()=>decodeFieldStrengthResult({...fixture.result,uncertainty_db:null,unit:'dBm'}));
});
test('integer minus zero matches legacy Python while explicit float retains sign', () => {
    const value = parseJSON('{"integer":-0,"float":-0.0}') as any;
    assert.equal(Object.is(value.integer, -0), false);
    assert.equal(Object.is(value.float, -0), true);
});
const bits = (value: number): string => { const a = new DataView(new ArrayBuffer(8)); a.setFloat64(0,value); return a.getBigUint64(0).toString(16).padStart(16,'0'); };

test('runtime fixtures reuse strict codecs and retain exact DTO fields and future metadata', () => {
    const fixture=parseJSON(readFileSync(new URL('../fixtures/runtime-v1.json',import.meta.url),'utf8')) as any;
    const status=decodeSecurityStatus(fixture.security_status);
    assert.equal(status.mode,'legacy_loopback'); assert.equal(status.pairing_required,false);
    const round=decodeSecurityStatus(parseJSON(encodeSecurityStatus(status))) as any;
    assert.equal(round.future_metadata.big,18446744073709551615n);
    assert.equal(bits(round.future_metadata.negative_zero),'8000000000000000');
    assert.equal(bits(round.future_metadata.adjacent),'bff0000000000001');
    assert.equal(round.future_metadata['é'],1); assert.equal(round.future_metadata['e\u0301'],2);
    assert.equal(round.future_metadata['$serde_json::private::Number'],'literal');
    assert.equal(decodeBrowserPairing(fixture.browser_pairing).credential,'browser_cookie');
    assert.equal(decodeNativePairing(fixture.native_pairing).token,'synthetic-fixture-never-a-valid-credential');
    const socket=decodeMeasurementWebSocket(fixture.websocket_descriptor);
    assert.equal(socket.path,'/api/v1/events/ws'); assert.equal(socket.subprotocol,'ori.measurements.v1.protobuf');
    socket.max_frame_bytes=18446744073709551615n;
    assert.equal(decodeMeasurementWebSocket(parseJSON(encodeMeasurementWebSocket(socket))).max_frame_bytes,18446744073709551615n);
    const page=decodeSweepSummaryPage(fixture.summary_page);
    assert.equal(page.next_cursor,null); assert.equal(bits(page.items[0]!.peak_level),'8000000000000000');
});

test('runtime backend and request feature selection fails closed', () => {
    assert.equal(runtimeOperations.find(op=>op.operation_id==='post_api_v1_security_pair')!.max_request_bytes,4096);
    assert.deepEqual(runtimeOperations.find(op=>op.operation_id==='get_api_v1_sessions_id_sweep_summaries')!.path_parameters,['id']);
    assert.deepEqual(runtimeOperations.find(op=>op.operation_id==='get_api_v1_events_ws')!.query,['after']);
    const ids=(backend:string,features:string[]=[],capabilities:string[]=[],request:any={})=>selectRuntimeOperations(backend,features,capabilities,request).map(op=>op.operation_id);
    assert.throws(()=>ids('unknown'));
    assert.equal(ids('python-legacy').length,136);
    assert.ok(!ids('rust-candidate').includes('post_api_v1_sessions_id_field_strength_rms_v2'));
    assert.ok(ids('rust-candidate',['field_strength_rms_v2']).includes('post_api_v1_sessions_id_field_strength_rms_v2'));
    assert.ok(!ids('python-legacy').includes('get_api_v1_security_status'));
    assert.ok(ids('rust-candidate').includes('get_api_v1_security_status'));
    assert.ok(!ids('rust-candidate').includes('get_api_v1_sessions_id_sweep_summaries'));
    assert.ok(ids('rust-candidate',['sweep_summaries']).includes('get_api_v1_sessions_id_sweep_summaries'));
    assert.ok(!ids('rust-candidate').includes('get_api_v1_events_ws'));
    assert.ok(ids('rust-candidate',[],['ori.measurements.v1.protobuf']).includes('get_api_v1_events_ws'));
    assert.ok(!ids('rust-candidate',['control_task_planning'],[],{enabled:true}).includes('post_api_v1_task_scheduler'));
    assert.ok(ids('rust-candidate',['control_task_planning'],[],{enabled:false}).includes('post_api_v1_task_scheduler'));
    assert.ok(ids('rust-candidate',['control_task_planning','control_task_scheduler'],[],{enabled:true}).includes('post_api_v1_task_scheduler'));
    assert.ok(!ids('rust-candidate',['control_task_planning','control_task_execution'],[],{enabled:true}).includes('post_api_v1_task_scheduler'));
});

test('runtime WebSocket descriptors reject an unknown protocol version', () => {
    const fixture=parseJSON(readFileSync(new URL('../fixtures/runtime-v1.json',import.meta.url),'utf8')) as any;
    assert.throws(()=>decodeMeasurementWebSocket({...fixture.websocket_descriptor,version:2}));
});

test('shared golden retains exact f64 bits, integerHz, IDs, UTC and original units', () => {
    const fixture = parseJSON(fixtureText) as any;
    const sweep = decodeSweep(fixture.sweep);
    assert.equal(sweep.id, 'fixture-sweep-α');
    assert.equal(sweep.session_id, 'fixture-сессия');
    assert.equal(sweep.stream_id, 'spectrum/one');
    assert.deepEqual(sweep.frequencies_hz, [100000001,100000002,100000003,100000004]);
    assert.deepEqual(sweep.levels.map(bits), ['c059080000000000','8000000000000000','bff0000000000001','c040600000000000']);
    assert.equal(sweep.interval.started_at, '2026-11-11T12:00:00.123456Z');
    assert.equal(sweep.interval.ended_at, '2026-11-11T12:00:00.223456Z');
    const round = decodeSweep(parseJSON(stringifyJSON(sweep)));
    assert.deepEqual(round.levels.map(bits), ['c059080000000000','8000000000000000','bff0000000000001','c040600000000000']);
    const observation = decodeReceiverObservation(fixture.observation);
    assert.equal(observation.stream_id, 'receiver+one');
    assert.equal(observation.level,137);
    assert.equal(observation.level_unit,'s_meter_count');
    assert.equal((observation.metadata as any).hardware_validated,false);
});

test('unknown metadata, explicit large integers and URL path IDs survive', () => {
    const value = parseJSON('{"id":"x/%+?&=雪","metadata":{"big":18446744073709551615,"nested":{"v":-0.0}},"new_field":[1,{"x":2}]}') as any;
    assert.equal(value.metadata.big,18446744073709551615n);
    const round = parseJSON(stringifyJSON(value)) as any;
    assert.equal(round.metadata.big,18446744073709551615n);
    assert.equal(bits(round.metadata.nested.v),'8000000000000000');
    assert.deepEqual(round.new_field,[1,{x:2}]);
    assert.throws(() => stringifyJSON({n:9007199254740992}));
    assert.equal(encodePathSegment(value.id),'x%2F%25%2B%3F%26%3D%E9%9B%AA');
    assert.equal(operationPath('/sessions/{id}/streams/{stream_id}',{id:'雪/?',stream_id:'r+one'}),'/sessions/%E9%9B%AA%2F%3F/streams/r%2Bone');
});

test('strict parser rejects all-depth duplicate names and malformed or nonfinite JSON', () => {
    for (const bad of ['{"x":1,"x":2}', '{"x":{"a":1,"\\u0061":2}}','[{"x":0,"x":1}]','{"x":NaN}','{"x":Infinity}','{"x":1e400}','{"x":1} trailing','[1,]','']) {
        assert.throws(() => parseJSON(bad),bad);
    }
    for (const n of [NaN,Infinity,-Infinity]) assert.throws(() => stringifyJSON({n}));
    assert.equal((parseJSON('{"雪":0}',new TextEncoder().encode('{"雪":0}').length) as any)['雪'],0);
    assert.throws(() => parseJSON('{"雪":0}',new TextEncoder().encode('{"雪":0}').length-1));
    assert.throws(() => stringifyJSON({n:undefined} as any));
    const cyclic: any = {}; cyclic.x=cyclic; assert.throws(() => stringifyJSON(cyclic));
});

test('UTC normalization uses exact integer microseconds across calendar and offset boundaries', () => {
    assert.equal(normalizeUTC('2026-11-11 12:00:00'),'2026-11-11T12:00:00.000000Z');
    assert.equal(normalizeUTC('2026-11-11T15:00:00.123456789+03:00'),'2026-11-11T12:00:00.123456Z');
    assert.equal(normalizeUTC('2026-01-01T00:00:00.000001+01:00'),'2025-12-31T23:00:00.000001Z');
    assert.equal(normalizeUTC('1969-12-31T23:59:59.999999Z'),'1969-12-31T23:59:59.999999Z');
    assert.throws(() => normalizeUTC('2026-02-30T12:00:00Z'));
    assert.throws(() => normalizeUTC('2026-11-11T12:00:00Z\n'));
    assert.throws(() => normalizeUTC('٢٠٢٦-11-11T12:00:00Z'));
});

test('typed encoders retain the full finite float64 range and exact Unicode code points', () => {
    const fixture = parseJSON(fixtureText) as any;
    const sweep = decodeSweep(fixture.sweep);
    sweep.levels = [Number.MIN_VALUE,Number.MAX_VALUE,-0,-1.0000000000000002];
    const round = decodeSweep(parseJSON(encodeSweep(sweep)));
    assert.deepEqual(round.levels.map(bits),['0000000000000001','7fefffffffffffff','8000000000000000','bff0000000000001']);
    const text = '{"é":1,"e\\u0301":2,"id":"é-e\\u0301"}';
    const value = parseJSON(text) as any;
    assert.equal(Object.keys(value).length,3);
    assert.deepEqual(new TextEncoder().encode(value.id),new Uint8Array([0xc3,0xa9,0x2d,0x65,0xcc,0x81]));
    assert.deepEqual(parseJSON(stringifyJSON(value)),value);
    assert.throws(() => parseJSON(new Uint8Array([0xff])));
    assert.throws(() => parseJSON('"\\ud800"'));
});

test('common envelopes retain error details and reject unsupported contract versions', () => {
    const error = decodeErrorEnvelope(parseJSON('{"contract":"openradioinspector-platform","contract_version":1,"error":{"code":"busy","message":"Receiver in use","retry_after":2}}'));
    assert.equal(error.error.code,'busy');
    assert.equal(error.error.message,'Receiver in use');
    assert.equal(error.error.retry_after,2);
    assert.throws(()=>decodeEnvelope(parseJSON('{"contract":"wrong","contract_version":1,"data":{}}')));
    assert.throws(()=>decodeEnvelope(parseJSON('{"contract":"openradioinspector-platform","contract_version":2,"data":{}}')));
});

test('shared metadata fixture preserves private names, literal escape prefixes and Unicode keys', () => {
    const text=readFileSync(new URL('metadata-edge-cases.json',import.meta.url),'utf8');
    const value=parseJSON(text) as any;
    assert.equal(value.sole['$serde_json::private::Number'],'1e999');
    assert.equal(value.multiple['$serde_json::private::Number'],'123');
    assert.equal(value.multiple.ordinary,true);
    assert.equal(value.prefix_collision['$serde_json::private::Number'],'raw-key');
    assert.equal(value.prefix_collision['$ori::escaped::$serde_json::private::Number'],'literal-prefix');
    assert.equal(value.prefix_collision['$ori::escaped::$ori::escaped::$serde_json::private::Number'],'double-prefix');
    assert.equal(value.huge_integer,18446744073709551616n);
    assert.equal(value.negative_integer,-18446744073709551616n);
    assert.equal(value.unicode['é']['e\u0301'],'ID/雪+%?');
    assert.equal(value.unicode['e\u0301'],'distinct');
    assert.equal(bits(value.negative_zero),'8000000000000000');
    assert.deepEqual(parseJSON(stringifyJSON(value)),value);
});
