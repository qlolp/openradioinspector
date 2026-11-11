import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {test} from 'node:test';
import {Float64, parseJSON, stringifyJSON, type JSONObject} from '../../../../contracts/generated/typescript/codec.ts';
import {decodeFrame, intoEnvelope, MAX_FRAME_BYTES, MAX_POINTS} from '../../../../contracts/generated/typescript/measurement_stream.ts';

const root = new URL('../../../../', import.meta.url);
const fixtures = JSON.parse(readFileSync(new URL('contracts/fixtures/stream-v1.json', root),'utf8'));
const wire = (text: string): Uint8Array => new Uint8Array(Buffer.from(text,'hex'));
const bits = (value: number | Float64): string => {const view=new DataView(new ArrayBuffer(8));view.setFloat64(0,value instanceof Float64?value.value:value);return Buffer.from(view.buffer).toString('hex');};
const sweep = (value: any): JSONObject => value.data.payload.sweep;

test('shared independent IDL oracle and exact structured metadata/arrays', () => {
  assert.equal(createHash('sha256').update(readFileSync(new URL('contracts/protobuf/measurement_stream.proto',root))).digest('hex'),fixtures.proto_sha256);
  for (const fixture of fixtures.valid) {
    const decoded=decodeFrame(wire(fixture.wire_hex));
    assert.equal(decoded.protocolVersion,1,fixture.name);
    assert.equal(decoded.sequence,BigInt(fixture.sequence),fixture.name);
    assert.equal(new TextDecoder().decode(decoded.envelopeJSON),fixture.metadata_json,fixture.name);
    assert.deepEqual(decoded.envelope,parseJSON(fixture.metadata_json),fixture.name);
    assert.equal(decoded.spectrum!==undefined,fixture.typed,fixture.name);
    if (decoded.spectrum) {
      assert.ok(decoded.spectrum.frequenciesHz instanceof BigUint64Array);
      assert.ok(decoded.spectrum.levels instanceof Float64Array);
      assert.deepEqual(Array.from(decoded.spectrum.frequenciesHz,String),fixture.frequencies_hz,fixture.name);
      assert.deepEqual(Array.from(decoded.spectrum.levels,bits),fixture.level_bits,fixture.name);
    }
    const restored=intoEnvelope(decoded) as any;
    const original=parseJSON(fixture.input_json) as any;
    if (fixture.typed) {
      const restoredSweep=sweep(restored), originalSweep=sweep(original);
      assert.deepEqual((restoredSweep.frequencies_hz as any[]).map(String),(originalSweep.frequencies_hz as any[]).map(String),fixture.name);
      assert.deepEqual((restoredSweep.levels as number[]).map(bits),(originalSweep.levels as number[]).map(bits),fixture.name);
      delete restoredSweep.levels; delete restoredSweep.frequencies_hz;
      delete originalSweep.levels; delete originalSweep.frequencies_hz;
    }
    assert.equal(stringifyJSON(restored),stringifyJSON(original),fixture.name);
  }
});

test('malformed protobuf, invalid JSON, duplicate markers and ambiguous arrays reject', () => {
  for (const fixture of fixtures.invalid) assert.throws(()=>decodeFrame(wire(fixture.wire_hex)),undefined,fixture.name);
});

test('every incomplete prefix rejects and lowering-only budgets apply', () => {
  const first=wire(fixtures.valid[0].wire_hex);
  for(let end=0;end<first.length;end++)assert.throws(()=>decodeFrame(first.subarray(0,end)),undefined,`prefix ${end}`);
  assert.throws(()=>decodeFrame(first,{maxFrameBytes:first.length-1,maxPoints:MAX_POINTS}));
  assert.throws(()=>decodeFrame(first,{maxFrameBytes:MAX_FRAME_BYTES,maxPoints:3}));
  assert.throws(()=>decodeFrame(first,{maxFrameBytes:MAX_FRAME_BYTES+1,maxPoints:MAX_POINTS}));
  assert.throws(()=>decodeFrame(first,{maxFrameBytes:MAX_FRAME_BYTES,maxPoints:MAX_POINTS+1}));
  const generic=fixtures.valid.find((entry:any)=>entry.name==='integer-level-kind-generic');
  assert.throws(()=>decodeFrame(wire(generic.wire_hex),{maxFrameBytes:MAX_FRAME_BYTES,maxPoints:2}));
  const unknown=fixtures.valid.find((entry:any)=>entry.name==='unknown-event-generic');
  assert.doesNotThrow(()=>decodeFrame(wire(unknown.wire_hex),{maxFrameBytes:MAX_FRAME_BYTES,maxPoints:0}));
});

test('returned metadata and arrays own their buffers even for a Node Buffer input', () => {
  const input=Buffer.from(fixtures.valid[0].wire_hex,'hex');
  const decoded=decodeFrame(input),metadata=Uint8Array.from(decoded.envelopeJSON);
  const frequencies=Array.from(decoded.spectrum!.frequenciesHz),levels=Array.from(decoded.spectrum!.levels,bits);
  input.fill(0);
  assert.deepEqual(decoded.envelopeJSON,metadata);
  assert.deepEqual(Array.from(decoded.spectrum!.frequenciesHz),frequencies);
  assert.deepEqual(Array.from(decoded.spectrum!.levels,bits),levels);
});
