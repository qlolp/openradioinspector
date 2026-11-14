import { expect, it, vi } from 'vitest';
import fixture from '../../../contracts/fixtures/stream-v1.json';
import { parseFrame, prepareSweep } from '../src/pipeline';
import { originalJSON, parseWire, sweepValue } from '../src/wire';
import type { JSONObject } from '../../../contracts/generated/typescript/index.ts';
const vector = (name: string) => fixture.valid.find((item) => item.name === name)!;
const bytes = (name: string) => Uint8Array.from(Buffer.from(vector(name).wire_hex, 'hex')).buffer;
it('decodes packed source buffers in the worker without stringify/reparse and transfers exact selected float bits', () => {
  const stringify = vi.spyOn(JSON, 'stringify');
  const frame = parseFrame(bytes('large-sequence-and-opaque-metadata'));
  expect(stringify).not.toHaveBeenCalled();
  stringify.mockRestore();
  expect(frame.sequence).toBe(9007199254740993n);
  expect(frame.sweep?.levels).toBeInstanceOf(Float64Array);
  expect(frame.sweep?.frequencies_hz).toBeInstanceOf(Float64Array);
  expect(frame.sweep?.source_indices).toBeInstanceOf(Uint32Array);
  expect(frame.sweep?.peak.index).toBe(1);
  expect(Object.is(frame.sweep?.levels[1], -0)).toBe(true);
  expect(frame.sweep).not.toHaveProperty('settings');
  const sweep = frame.sweep!;
  const transfer = [
    (sweep.levels as Float64Array).buffer,
    (sweep.frequencies_hz as Float64Array).buffer,
    (sweep.source_indices as Uint32Array).buffer,
  ];
  const copied = structuredClone(frame, { transfer });
  expect(Object.is(copied.sweep?.levels[1], -0)).toBe(true);
  expect((sweep.levels as Float64Array).byteLength).toBe(0);
});
it('accepts valid future-range and generic source envelopes without creating an imprecise plot', () => {
  for (const name of ['uint64-limits-and-f64-bits', 'larger-than-uint64-hz-generic']) {
    const frame = parseFrame(bytes(name));
    expect(frame.sweep).toBeUndefined();
    expect(frame.unsupported?.id).toBeTruthy();
    expect(typeof frame.unsupported?.frequency_hz).toBe('bigint');
  }
  const raw = parseWire(vector('larger-than-uint64-hz-generic').input_json) as JSONObject;
  const sweep = sweepValue(((raw.data as JSONObject).payload as JSONObject).sweep);
  const exported = parseWire(originalJSON(sweep)) as JSONObject;
  expect(exported.frequencies_hz).toEqual(
    (((raw.data as JSONObject).payload as JSONObject).sweep as JSONObject).frequencies_hz,
  );
  expect(parseFrame(bytes('unknown-event-generic')).sequence).toBe(4n);
});
it('rejects malformed binary before a frame can reach the sequence gate', () => {
  for (const name of [
    'duplicate-sequence',
    'duplicate-spectrum',
    'mismatched-array-length',
    'nan-level',
  ]) {
    const invalid = fixture.invalid.find((item) => item.name === name);
    expect(invalid, name).toBeDefined();
    expect(() =>
      parseFrame(Uint8Array.from(Buffer.from(invalid!.wire_hex, 'hex')).buffer),
    ).toThrow();
  }
});

it('keeps opaque optional point-count metadata out of the render copy', () => {
  const raw = parseWire(vector('measurement-v1').input_json) as JSONObject;
  const source = sweepValue(((raw.data as JSONObject).payload as JSONObject).sweep);
  const opaque = { value: 1, future: 'opaque extension' };
  const extended = { ...source, original_point_count: opaque } as unknown as typeof source;
  const prepared = prepareSweep(extended).sweep!;
  expect(prepared.original_point_count).toBe(source.levels.length);
  expect(extended.original_point_count).toBe(opaque);
  expect(prepareSweep({ ...source, original_point_count: 4000 }).sweep!.original_point_count).toBe(
    4000,
  );
});
