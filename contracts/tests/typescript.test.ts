import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parseJSON, stringifyJSON, decodeSweep, decodeReceiverObservation, normalizeUTC, encodePathSegment, operationPath, encodeSweep, decodeEnvelope, decodeErrorEnvelope} from '../generated/typescript/index.ts';

const fixtureText = readFileSync(new URL('../fixtures/measurement-v1.json', import.meta.url), 'utf8');
const bits = (value: number): string => { const a = new DataView(new ArrayBuffer(8)); a.setFloat64(0,value); return a.getBigUint64(0).toString(16).padStart(16,'0'); };

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
